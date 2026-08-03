"""Cursor-paginated operational CRM queue across all ticket sources.

The queue deliberately anchors only record creation to ``as_of``. Mutable
fields (status, assignment, category and SLA evidence) remain live between
pages; callers must not mistake this contract for a historical snapshot.

Language and SLA semantics stay owned by ``operational_intelligence``.  This
module reuses its source adapters and open-state predicate instead of growing a
fourth, subtly different ticket vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import heapq
import json
from typing import Any, Callable, Mapping

from flask import current_app
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import and_, case, false, func, or_

from models import MunicipioTicket, PymeTicket, TenantProfile, TenantTicket, User
from services.crm_operational_queue_guard import (
    QueueScanBudget,
    configured_queue_scan_budget,
)
from services.employee_routing import employee_scope
from services.operational_intelligence import (
    _aware_datetime,
    _municipio_ticket_query,
    _municipio_ticket_record,
    _open_status_query,
    _pyme_ticket_record,
    _tenant_ticket_record,
)
from utils.roles import ROLE_EMPLEADO, canonical_role


CONTRACT_VERSION = "inbox.operational_queue.v1"
METRIC_CONTRACT_VERSION = "operations.queue_truth.v1"
GRAIN = "one_current_open_source_record"
STATE_CONSISTENCY = "created_at_anchored_live_state"
CURSOR_VERSION = "crm.operational_queue.cursor.v1"
CURSOR_SALT = "chatboc.crm-operational-queue.cursor.v1"
DEFAULT_CURSOR_TTL_SECONDS = 15 * 60
DEFAULT_LIMIT = 50
MAX_LIMIT = 100

SOURCE_MODELS = ("TenantTicket", "MunicipioTicket", "PymeTicket")
SLA_FILTERS = {"breached", "at_risk", "unknown", "healthy", "not_eligible"}
AGE_FILTERS = {
    "lt_1h",
    "1h_4h",
    "4h_24h",
    "1d_3d",
    "3d_7d",
    "gte_7d",
    "unknown",
}
_ALLOWED_QUERY_KEYS = {
    "age",
    "assignee",
    "category",
    "cursor",
    "limit",
    "queue",
    "sla",
    "source_model",
    "tenant",
    "tenant_id",
    "tenant_slug",
}


class OperationalQueueError(ValueError):
    """A fail-closed public queue-contract error."""

    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        status_code: int = 400,
        action_hint: str = "fix_queue_request",
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status_code = status_code
        self.action_hint = action_hint


@dataclass(frozen=True)
class QueueRequest:
    filters: dict[str, Any]
    limit: int
    cursor: str | None


@dataclass(frozen=True)
class _ActorScope:
    mode: str
    category_names: tuple[str, ...]
    category_ids: tuple[int, ...]
    digest: str


@dataclass(frozen=True)
class _SourceSpec:
    source_model: str
    source_rank: int
    model: type
    created_column: Any
    status_column: Any
    category_column: Any
    category_id_column: Any | None
    assignee_column: Any | None
    adapter: Callable[..., dict[str, Any]]


_SOURCE_SPECS = (
    _SourceSpec(
        source_model="TenantTicket",
        source_rank=1,
        model=TenantTicket,
        created_column=TenantTicket.created_at,
        status_column=TenantTicket.estado,
        category_column=TenantTicket.categoria,
        category_id_column=None,
        assignee_column=None,
        adapter=_tenant_ticket_record,
    ),
    _SourceSpec(
        source_model="MunicipioTicket",
        source_rank=2,
        model=MunicipioTicket,
        created_column=MunicipioTicket.fecha,
        status_column=MunicipioTicket.estado,
        category_column=MunicipioTicket.categoria,
        category_id_column=MunicipioTicket.categoria_id,
        assignee_column=MunicipioTicket.asignado_a_id,
        adapter=_municipio_ticket_record,
    ),
    _SourceSpec(
        source_model="PymeTicket",
        source_rank=3,
        model=PymeTicket,
        created_column=PymeTicket.fecha,
        status_column=PymeTicket.estado,
        category_column=PymeTicket.categoria,
        category_id_column=PymeTicket.categoria_id,
        assignee_column=PymeTicket.asignado_a_id,
        adapter=_pyme_ticket_record,
    ),
)
_SOURCE_SPEC_BY_NAME = {spec.source_model: spec for spec in _SOURCE_SPECS}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _positive_int(value: Any, *, reason_code: str, field: str) -> int:
    if isinstance(value, bool):
        raise OperationalQueueError(reason_code, f"{field} debe ser un entero positivo")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OperationalQueueError(reason_code, f"{field} debe ser un entero positivo") from exc
    if parsed <= 0:
        raise OperationalQueueError(reason_code, f"{field} debe ser un entero positivo")
    return parsed


def _single_query_value(args: Mapping[str, Any], key: str) -> Any:
    getlist = getattr(args, "getlist", None)
    if callable(getlist):
        values = list(getlist(key))
        if len(values) > 1:
            raise OperationalQueueError(
                "invalid_queue_filter",
                f"El filtro {key} no puede repetirse",
            )
    return args.get(key)


def parse_queue_request(args: Mapping[str, Any]) -> QueueRequest:
    """Parse the public query contract without silently widening filters."""

    unknown = sorted(set(args.keys()) - _ALLOWED_QUERY_KEYS)
    if unknown:
        raise OperationalQueueError(
            "invalid_queue_filter",
            f"Filtros no soportados: {', '.join(unknown)}",
        )

    raw_limit = _single_query_value(args, "limit")
    if raw_limit is None:
        limit = DEFAULT_LIMIT
    else:
        limit = _positive_int(raw_limit, reason_code="invalid_queue_filter", field="limit")
        if limit > MAX_LIMIT:
            raise OperationalQueueError(
                "invalid_queue_filter",
                f"limit no puede superar {MAX_LIMIT}",
            )

    raw_queue = _single_query_value(args, "queue")
    if raw_queue is not None and str(raw_queue).strip() != "open":
        raise OperationalQueueError("invalid_queue_filter", "Filtro queue invalido")
    # Keep the complete canonical shape stable for frontend parsing and cursor
    # binding.  Missing optional filters are represented explicitly as null.
    filters: dict[str, Any] = {
        "queue": "open",
        "sla": None,
        "age": None,
        "assignee": None,
        "source_model": None,
        "category": None,
    }

    raw_sla = _single_query_value(args, "sla")
    if raw_sla is not None:
        sla = str(raw_sla).strip()
        if sla not in SLA_FILTERS:
            raise OperationalQueueError("invalid_queue_filter", "Filtro sla invalido")
        filters["sla"] = sla

    raw_age = _single_query_value(args, "age")
    if raw_age is not None:
        age = str(raw_age).strip()
        if age not in AGE_FILTERS:
            raise OperationalQueueError("invalid_queue_filter", "Filtro age invalido")
        filters["age"] = age

    raw_assignee = _single_query_value(args, "assignee")
    if raw_assignee is not None:
        assignee = str(raw_assignee).strip()
        if assignee == "unassigned":
            filters["assignee"] = assignee
        else:
            filters["assignee"] = _positive_int(
                assignee,
                reason_code="invalid_queue_filter",
                field="assignee",
            )

    raw_source_model = _single_query_value(args, "source_model")
    if raw_source_model is not None:
        source_model = str(raw_source_model).strip()
        if source_model not in SOURCE_MODELS:
            raise OperationalQueueError("invalid_queue_filter", "Filtro source_model invalido")
        filters["source_model"] = source_model

    raw_category = _single_query_value(args, "category")
    if raw_category is not None:
        category = _normalized_text(raw_category)
        if not category or len(category) > 100:
            raise OperationalQueueError("invalid_queue_filter", "Filtro category invalido")
        filters["category"] = category

    raw_cursor = _single_query_value(args, "cursor")
    cursor = None
    if raw_cursor is not None:
        cursor = str(raw_cursor).strip()
        if not cursor or len(cursor) > 8192:
            raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")

    return QueueRequest(filters=filters, limit=limit, cursor=cursor)


def _json_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _actor_scope(actor: User) -> _ActorScope:
    role = canonical_role(getattr(actor, "rol", None))
    if role != ROLE_EMPLEADO:
        descriptor = {"mode": "tenant_wide", "role": role}
        return _ActorScope("tenant_wide", tuple(), tuple(), _json_digest(descriptor))

    names: list[str] = []
    ids: list[int] = []

    configured_scope = employee_scope(actor)
    names.extend(configured_scope.get("categorias") or [])

    categories = getattr(actor, "categorias_ticket", None) or []
    for category in categories:
        name = _normalized_text(getattr(category, "nombre", None))
        if name:
            names.append(name)
        category_id = getattr(category, "id", None)
        if isinstance(category_id, int) and category_id > 0:
            ids.append(category_id)

    configured_csv = str(getattr(actor, "ticket_categorias", None) or "")
    names.extend(_normalized_text(item) for item in configured_csv.split(","))

    normalized_names = tuple(dict.fromkeys(name for name in names if name))
    normalized_ids = tuple(dict.fromkeys(ids))
    descriptor = {
        "mode": "employee_categories",
        "role": role,
        "category_names": sorted(normalized_names),
        "category_ids": sorted(normalized_ids),
    }
    return _ActorScope(
        "employee_categories",
        normalized_names,
        normalized_ids,
        _json_digest(descriptor),
    )


def _cursor_serializer() -> URLSafeSerializer:
    secret = current_app.config.get("CRM_OPERATIONAL_QUEUE_CURSOR_SECRET") or current_app.secret_key
    if not secret:
        raise OperationalQueueError(
            "queue_cursor_configuration_unavailable",
            "La firma de cursor no esta configurada",
            status_code=503,
            action_hint="configure_queue_cursor_secret",
        )
    return URLSafeSerializer(secret_key=secret, salt=CURSOR_SALT)


def _cursor_ttl_seconds() -> int:
    raw = current_app.config.get(
        "CRM_OPERATIONAL_QUEUE_CURSOR_TTL_SECONDS",
        DEFAULT_CURSOR_TTL_SECONDS,
    )
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        parsed = DEFAULT_CURSOR_TTL_SECONDS
    return max(60, min(parsed, 24 * 60 * 60))


def _cursor_datetime(value: datetime | None) -> str | None:
    normalized = _aware_datetime(value)
    if normalized is None:
        return None
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_cursor_datetime(value: Any, *, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido") from exc
    normalized = _aware_datetime(parsed)
    if normalized is None:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")
    return normalized


def _encode_cursor(
    *,
    tenant_id: int,
    actor_id: int,
    scope_hash: str,
    filters_hash: str,
    as_of: datetime,
    positions: dict[str, dict[str, Any]],
) -> str:
    now = _utc_now()
    expires_at = now + timedelta(seconds=_cursor_ttl_seconds())
    payload = {
        "v": CURSOR_VERSION,
        "tenant_id": tenant_id,
        "actor_id": actor_id,
        "scope_hash": scope_hash,
        "filters_hash": filters_hash,
        "as_of": _cursor_datetime(as_of),
        "positions": positions,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    return _cursor_serializer().dumps(payload)


def _decode_cursor(
    token: str,
    *,
    tenant_id: int,
    actor_id: int,
    scope_hash: str,
    filters_hash: str,
) -> tuple[datetime, dict[str, dict[str, Any]]]:
    try:
        payload = _cursor_serializer().loads(token)
    except BadSignature as exc:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido") from exc
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")

    try:
        payload_tenant_id = int(payload.get("tenant_id"))
        payload_actor_id = int(payload.get("actor_id"))
        issued_at = int(payload.get("iat"))
        expires_at = int(payload.get("exp"))
    except (TypeError, ValueError) as exc:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido") from exc

    if payload_tenant_id != tenant_id or payload_actor_id != actor_id:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")
    if payload.get("scope_hash") != scope_hash or payload.get("filters_hash") != filters_hash:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")

    now_epoch = int(_utc_now().timestamp())
    if expires_at <= now_epoch:
        raise OperationalQueueError(
            "queue_cursor_expired",
            "El cursor expiro",
            status_code=410,
            action_hint="restart_queue_pagination",
        )
    if issued_at > now_epoch + 60 or expires_at <= issued_at:
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")

    as_of = _parse_cursor_datetime(payload.get("as_of"))
    raw_positions = payload.get("positions")
    if not isinstance(raw_positions, dict) or set(raw_positions) - set(SOURCE_MODELS):
        raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")

    positions: dict[str, dict[str, Any]] = {}
    for source_model, raw_position in raw_positions.items():
        if not isinstance(raw_position, dict):
            raise OperationalQueueError("invalid_queue_cursor", "Cursor invalido")
        source_id = _positive_int(
            raw_position.get("source_id"),
            reason_code="invalid_queue_cursor",
            field="source_id",
        )
        created_at = _parse_cursor_datetime(raw_position.get("created_at"), allow_none=True)
        positions[source_model] = {"created_at": created_at, "source_id": source_id}
    return as_of, positions


def _normalized_category_expression(column: Any):
    return func.lower(func.trim(func.coalesce(column, "")))


def _apply_actor_scope(query: Any, spec: _SourceSpec, scope: _ActorScope):
    if scope.mode != "employee_categories":
        return query
    conditions = []
    if scope.category_names:
        conditions.append(_normalized_category_expression(spec.category_column).in_(scope.category_names))
    if scope.category_ids and spec.category_id_column is not None:
        conditions.append(spec.category_id_column.in_(scope.category_ids))
    if not conditions:
        return query.filter(false())
    return query.filter(or_(*conditions))


def _apply_category_filter(query: Any, spec: _SourceSpec, category: str | None):
    if not category:
        return query
    normalized = _normalized_category_expression(spec.category_column)
    if category == "sin_categoria":
        return query.filter(normalized == "")
    return query.filter(normalized == category)


def _apply_age_filter(query: Any, spec: _SourceSpec, age: str | None, *, as_of: datetime):
    """Push the public half-open age buckets into every native date column."""

    if not age:
        return query
    column = spec.created_column
    if age == "unknown":
        return query.filter(column.is_(None))

    one_hour = as_of - timedelta(hours=1)
    four_hours = as_of - timedelta(hours=4)
    one_day = as_of - timedelta(days=1)
    three_days = as_of - timedelta(days=3)
    seven_days = as_of - timedelta(days=7)
    if age == "lt_1h":
        return query.filter(column > one_hour)
    if age == "1h_4h":
        return query.filter(column <= one_hour, column > four_hours)
    if age == "4h_24h":
        return query.filter(column <= four_hours, column > one_day)
    if age == "1d_3d":
        return query.filter(column <= one_day, column > three_days)
    if age == "3d_7d":
        return query.filter(column <= three_days, column > seven_days)
    return query.filter(column <= seven_days)


def _apply_native_assignee_filter(
    query: Any,
    spec: _SourceSpec,
    assignee: str | int | None,
):
    """Push assignment only where it is a native column, preserving legacy values."""

    column = spec.assignee_column
    if assignee is None or column is None:
        return query
    if assignee == "unassigned":
        # Canonical public semantics treat null and non-positive legacy IDs as
        # unassigned.  Keep the SQL predicate identical to `_coerce_assignee_id`.
        return query.filter(or_(column.is_(None), column <= 0))
    return query.filter(column == int(assignee))


def _base_query(
    spec: _SourceSpec,
    *,
    tenant: TenantProfile,
    scope: _ActorScope,
    filters: Mapping[str, Any],
    as_of: datetime,
):
    if spec.source_model == "TenantTicket":
        query = TenantTicket.query.filter(TenantTicket.tenant_id == tenant.id)
    elif spec.source_model == "MunicipioTicket":
        query = _municipio_ticket_query(tenant)
    else:
        query = PymeTicket.query.filter(PymeTicket.tenant_id == tenant.id)

    query = _open_status_query(query, spec.status_column)
    query = query.filter(or_(spec.created_column.is_(None), spec.created_column <= as_of))
    query = _apply_actor_scope(query, spec, scope)
    query = _apply_category_filter(query, spec, filters.get("category"))
    query = _apply_age_filter(query, spec, filters.get("age"), as_of=as_of)
    query = _apply_native_assignee_filter(query, spec, filters.get("assignee"))
    return query


def _after_position(query: Any, spec: _SourceSpec, position: dict[str, Any] | None):
    if not position:
        return query
    source_id = int(position["source_id"])
    created_at = position.get("created_at")
    if created_at is None:
        return query.filter(
            and_(spec.created_column.is_(None), spec.model.id < source_id)
        )
    return query.filter(
        or_(
            spec.created_column < created_at,
            spec.created_column.is_(None),
            and_(spec.created_column == created_at, spec.model.id < source_id),
        )
    )


def _position_for_row(spec: _SourceSpec, row: Any) -> dict[str, Any]:
    return {
        "created_at": _aware_datetime(getattr(row, spec.created_column.key, None)),
        "source_id": int(row.id),
    }


def _age_bucket(created_at: Any, as_of: datetime) -> str:
    created = _aware_datetime(created_at)
    if created is None:
        return "unknown"
    seconds = max(0, int((as_of - created).total_seconds()))
    if seconds < 60 * 60:
        return "lt_1h"
    if seconds < 4 * 60 * 60:
        return "1h_4h"
    if seconds < 24 * 60 * 60:
        return "4h_24h"
    if seconds < 3 * 24 * 60 * 60:
        return "1d_3d"
    if seconds < 7 * 24 * 60 * 60:
        return "3d_7d"
    return "gte_7d"


def _coerce_assignee_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _canonicalize_record(spec: _SourceSpec, record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize legacy adapter values before filtering or public serialization."""

    canonical = dict(record)
    source_id = int(canonical["id"])
    raw_title = canonical.get("title")
    title = str(raw_title).strip() if raw_title is not None else ""
    if not title:
        noun = "Reclamo" if spec.source_model == "MunicipioTicket" else "Ticket"
        title = f"{noun} {source_id}"
    canonical["title"] = title
    canonical["assignee_id"] = _coerce_assignee_id(canonical.get("assignee_id"))
    return canonical


def _record_matches(record: Mapping[str, Any], filters: Mapping[str, Any], *, as_of: datetime) -> bool:
    if filters.get("category") and record.get("category") != filters["category"]:
        return False

    requested_assignee = filters.get("assignee")
    if requested_assignee is not None:
        canonical_assignee = _coerce_assignee_id(record.get("assignee_id"))
        if requested_assignee == "unassigned":
            if canonical_assignee is not None:
                return False
        elif canonical_assignee != requested_assignee:
            return False

    requested_sla = filters.get("sla")
    if requested_sla and (record.get("sla") or {}).get("state") != requested_sla:
        return False

    requested_age = filters.get("age")
    if requested_age and _age_bucket(record.get("created_at"), as_of) != requested_age:
        return False
    return True


def _serialize_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": _cursor_datetime(position.get("created_at")),
        "source_id": int(position["source_id"]),
    }


def _detail_endpoint(source_model: str, source_id: int) -> str:
    if source_model == "TenantTicket":
        return f"/api/v2/tickets/{source_id}"
    if source_model == "MunicipioTicket":
        return f"/api/v2/inbox/omnichannel/{source_id}?source_model=MunicipioTicket"
    return f"/api/tickets/pyme/{source_id}"


def _serialize_record(spec: _SourceSpec, record: Mapping[str, Any], *, as_of: datetime) -> dict[str, Any]:
    record = _canonicalize_record(spec, record)
    source_id = int(record["id"])
    created_at = _aware_datetime(record.get("created_at"))
    updated_at = _aware_datetime(record.get("updated_at"))
    return {
        "queue_id": f"{spec.source_model}:{source_id}",
        "source_model": spec.source_model,
        "source_id": source_id,
        "title": record.get("title"),
        "status": record.get("status"),
        "category": record.get("category"),
        "channel": record.get("channel"),
        "priority": record.get("priority"),
        "assignee_id": record.get("assignee_id"),
        "created_at": _cursor_datetime(created_at),
        "updated_at": _cursor_datetime(updated_at),
        "age_bucket": _age_bucket(created_at, as_of),
        "sla": dict(record.get("sla") or {}),
        "detail_endpoint": _detail_endpoint(spec.source_model, source_id),
    }


def _datetime_order_value(value: datetime | None) -> int:
    if value is None:
        return 0
    normalized = _aware_datetime(value)
    if normalized is None:
        return 0
    return (
        normalized.toordinal() * 86_400_000_000
        + normalized.hour * 3_600_000_000
        + normalized.minute * 60_000_000
        + normalized.second * 1_000_000
        + normalized.microsecond
    )


class _SourceStream:
    """Bounded-memory keyset reader with one unconsumed matching head."""

    def __init__(
        self,
        spec: _SourceSpec,
        *,
        tenant: TenantProfile,
        scope: _ActorScope,
        filters: Mapping[str, Any],
        as_of: datetime,
        initial_position: dict[str, Any] | None,
        chunk_size: int,
        scan_budget: QueueScanBudget,
    ) -> None:
        self.spec = spec
        self.tenant = tenant
        self.scope = scope
        self.filters = filters
        self.as_of = as_of
        self.chunk_size = chunk_size
        self.scan_budget = scan_budget
        self.safe_position = dict(initial_position) if initial_position else None
        self.query_position = dict(initial_position) if initial_position else None
        self._buffer: list[Any] = []
        self._buffer_index = 0
        self._pending: tuple[dict[str, Any], dict[str, Any]] | None = None
        self._exhausted = False

    def _load_chunk(self) -> None:
        query = _base_query(
            self.spec,
            tenant=self.tenant,
            scope=self.scope,
            filters=self.filters,
            as_of=self.as_of,
        )
        query = _after_position(query, self.spec, self.query_position)
        null_rank = case((self.spec.created_column.is_(None), 1), else_=0)
        self._buffer = (
            query.order_by(
                null_rank.asc(),
                self.spec.created_column.desc(),
                self.spec.model.id.desc(),
            )
            .limit(self.chunk_size)
            .all()
        )
        self._buffer_index = 0
        if not self._buffer:
            self._exhausted = True

    def next_match(self) -> dict[str, Any] | None:
        if self._pending is not None:
            return self._pending[0]
        while not self._exhausted:
            if self._buffer_index >= len(self._buffer):
                self._load_chunk()
                if self._exhausted:
                    break
            row = self._buffer[self._buffer_index]
            self._buffer_index += 1
            self.scan_budget.consume(source_model=self.spec.source_model)
            position = _position_for_row(self.spec, row)
            self.query_position = position
            record = _canonicalize_record(
                self.spec,
                self.spec.adapter(row, as_of=self.as_of),
            )
            if _record_matches(record, self.filters, as_of=self.as_of):
                self._pending = (record, position)
                return record
            self.safe_position = position
        return None

    def consume_pending(self) -> None:
        if self._pending is None:
            return
        self.safe_position = self._pending[1]
        self._pending = None

    def cursor_position(self) -> dict[str, Any] | None:
        return _serialize_position(self.safe_position) if self.safe_position else None


def _heap_item(stream: _SourceStream, record: Mapping[str, Any]):
    created_at = _aware_datetime(record.get("created_at"))
    return (
        1 if created_at is None else 0,
        -_datetime_order_value(created_at),
        stream.spec.source_rank,
        -int(record["id"]),
        stream,
        record,
    )


def build_operational_queue(
    *,
    tenant: TenantProfile,
    actor: User,
    queue_request: QueueRequest,
) -> dict[str, Any]:
    """Build one exact, bounded-memory page of the cross-source open queue."""

    tenant_id = _positive_int(tenant.id, reason_code="invalid_queue_scope", field="tenant_id")
    actor_id = _positive_int(actor.id, reason_code="invalid_queue_scope", field="actor_id")
    scope = _actor_scope(actor)
    filters_hash = _json_digest(queue_request.filters)

    if queue_request.cursor:
        as_of, positions = _decode_cursor(
            queue_request.cursor,
            tenant_id=tenant_id,
            actor_id=actor_id,
            scope_hash=scope.digest,
            filters_hash=filters_hash,
        )
    else:
        as_of = _utc_now()
        positions = {}

    source_filter = queue_request.filters.get("source_model")
    selected_specs = [
        spec for spec in _SOURCE_SPECS
        if source_filter is None or spec.source_model == source_filter
    ]
    post_query_filters: list[str] = []
    if queue_request.filters.get("sla"):
        post_query_filters.append("sla:adapter_metadata")
    if queue_request.filters.get("assignee") is not None and any(
        spec.assignee_column is None for spec in selected_specs
    ):
        post_query_filters.append("assignee:TenantTicket.datos_extra")
    scan_budget = configured_queue_scan_budget(
        post_query_filters=post_query_filters,
    )
    chunk_size = max(10, min(MAX_LIMIT, queue_request.limit * 2))
    streams = [
        _SourceStream(
            spec,
            tenant=tenant,
            scope=scope,
            filters=queue_request.filters,
            as_of=as_of,
            initial_position=positions.get(spec.source_model),
            chunk_size=chunk_size,
            scan_budget=scan_budget,
        )
        for spec in selected_specs
    ]

    heap: list[tuple[Any, ...]] = []
    for stream in streams:
        record = stream.next_match()
        if record is not None:
            heapq.heappush(heap, _heap_item(stream, record))

    items: list[dict[str, Any]] = []
    while heap and len(items) < queue_request.limit:
        *_sort, stream, record = heapq.heappop(heap)
        items.append(_serialize_record(stream.spec, record, as_of=as_of))
        stream.consume_pending()
        next_record = stream.next_match()
        if next_record is not None:
            heapq.heappush(heap, _heap_item(stream, next_record))

    has_more = bool(heap)
    next_cursor = None
    if has_more:
        next_positions = {
            stream.spec.source_model: position
            for stream in streams
            if (position := stream.cursor_position()) is not None
        }
        next_cursor = _encode_cursor(
            tenant_id=tenant_id,
            actor_id=actor_id,
            scope_hash=scope.digest,
            filters_hash=filters_hash,
            as_of=as_of,
            positions=next_positions,
        )

    source_counts: dict[str, int] = {source: 0 for source in SOURCE_MODELS}
    for item in items:
        source_counts[item["source_model"]] += 1

    return {
        "contract_version": CONTRACT_VERSION,
        "metric_contract": METRIC_CONTRACT_VERSION,
        "grain": GRAIN,
        "tenant_slug": str(tenant.slug or ""),
        "scope_fingerprint": scope.digest,
        "filters_fingerprint": filters_hash,
        "as_of": _cursor_datetime(as_of),
        "state_consistency": STATE_CONSISTENCY,
        "consistency": {
            "creation_membership": "created_at_null_or_lte_as_of",
            "null_created_at": "included_ordered_last",
            "mutable_fields": "live_at_each_page_read",
            "historical_snapshot": False,
            "durable_revision": False,
        },
        "sort": ["created_at:desc", "source_rank:asc", "source_id:desc"],
        "filters": dict(queue_request.filters),
        "access_scope": {
            "mode": scope.mode,
            "category_count": len(scope.category_names) + len(scope.category_ids),
        },
        "items": items,
        "page": {
            "limit": queue_request.limit,
            "returned": len(items),
            "has_more": has_more,
            "next_cursor": next_cursor,
            "source_counts": source_counts,
        },
    }


__all__ = [
    "AGE_FILTERS",
    "CONTRACT_VERSION",
    "CURSOR_VERSION",
    "GRAIN",
    "METRIC_CONTRACT_VERSION",
    "OperationalQueueError",
    "QueueRequest",
    "SLA_FILTERS",
    "SOURCE_MODELS",
    "STATE_CONSISTENCY",
    "build_operational_queue",
    "parse_queue_request",
]
