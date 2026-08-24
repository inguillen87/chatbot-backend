"""Analytics blueprint exposing CRM dashboards and data endpoints."""

from __future__ import annotations

from datetime import datetime
from functools import wraps
import json
from typing import Any
import time
import uuid

from flask import Blueprint, abort, current_app, g, jsonify, render_template, request
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.exceptions import HTTPException

from extensions import db

from services.analytics.cache import analytics_cache
from services.analytics.config import get_config
from services.analytics.filters import AnalyticsFilters, parse_filters
from services.analytics import (
    get_breakdown,
    get_cohorts,
    get_geo_heatmap,
    get_geo_points,
    get_operations_overview,
    get_summary,
    get_timeseries,
    get_top,
    get_whatsapp_templates,
)
from services.analytics.ingestor import analytics_ingestor
from services.analytics.models import AnalyticsModuleStatus
from models import AnalyticsEventV2, TenantProfile
from services.analytics.rbac import (
    legacy_tenant_wide_analytics_denial,
    require_access,
)
from services.tenant_ticket_scope import (
    TicketTenantScopeError,
    resolve_unique_tenant_for_owner,
)

analytics_bp = Blueprint("analytics", __name__, url_prefix="/analytics")

ANALYTICS_IDENTITY_COVERAGE_CONTRACT_VERSION = "analytics.identity_coverage.v1"
ANALYTICS_EVENT_INGEST_CONTRACT_VERSION = "analytics.event_ingest.v1"
ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION = "analytics.event_schema.v1"
ANALYTICS_GEO_LAYERS_CONTRACT_VERSION = "analytics.geo_layers.v1"

ANALYTICS_CANONICAL_EVENT_NAMES = [
    "message_received",
    "ticket_created",
    "ticket_assigned",
    "ticket_resolved",
    "survey_answer_submitted",
    "vote_submitted",
    "product_viewed",
    "catalog_viewed",
    "whatsapp_cta_clicked",
    "assisted_upload_started",
    "assisted_upload_submitted",
    "cart_started",
    "checkout_previewed",
    "checkout_session_created",
    "order_created",
    "order_tracking_opened",
    "flow_runtime_opened",
    "flow_action_requested",
    "flow_webview_opened",
    "flow_webview_completed",
    "flow_webview_failed",
    "location_shared",
    "widget_session_opened",
    "portal_session_opened",
]


def _analytics_request_id() -> str:
    raw = request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return f"req_{uuid.uuid4().hex}"


def _analytics_cors_origin() -> str | None:
    origin = (request.headers.get("Origin") or "").strip()
    if not origin:
        return None
    allowed = current_app.config.get("CORS_ALLOWED_ORIGINS") or current_app.config.get("ALLOWED_ORIGINS") or []
    if isinstance(allowed, str):
        allowed = [item.strip() for item in allowed.split(",") if item.strip()]
    normalized = origin.lower()
    if (
        origin in allowed
        or normalized == "https://www.chatboc.ar"
        or normalized.endswith(".chatboc.ar")
        or normalized.startswith("http://localhost")
        or normalized.startswith("http://127.0.0.1")
    ):
        return origin
    return None


@analytics_bp.before_request
def _ensure_feature_enabled():
    if request.method == "OPTIONS":
        response = jsonify({"ok": True, "request_id": _analytics_request_id()})
        response.headers["X-Request-Id"] = _analytics_request_id()
        return response
    config = get_config()
    if not config.feature_enabled:
        abort(404)
    return None


@analytics_bp.after_request
def _analytics_add_cors(response):
    origin = _analytics_cors_origin()
    if origin:
        response.headers.setdefault("Access-Control-Allow-Origin", origin)
        response.headers.setdefault("Access-Control-Allow-Credentials", "true")
        response.headers.setdefault("Vary", "Origin")
        response.headers.setdefault("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Authorization, Content-Type, X-Requested-With, X-Request-Id, X-Entity-Token, X-Tenant-Slug, X-Debug-Tenant",
        )
        response.headers.setdefault("Access-Control-Expose-Headers", "X-Request-Id")
    response.headers.setdefault("X-Request-Id", _analytics_request_id())
    return response






def _tenant_id_from_filters(filters: AnalyticsFilters) -> int:
    try:
        return int(filters.tenant_id)
    except (TypeError, ValueError):
        return 0


def _requested_tenant_slug() -> str:
    return (request.args.get("tenant_slug") or request.args.get("tenant") or "").strip().lower()


_ANALYTICS_SCOPE_ALIASES = {
    "municipio": "municipio",
    "municipal": "municipio",
    "municipality": "municipio",
    "pyme": "pyme",
    "empresa": "pyme",
    "business": "pyme",
    "operaciones": "operaciones",
    "operations": "operaciones",
}


def _analytics_tenant_resolution_error(
    code: str,
    message: str,
    *,
    status: int = 400,
    **details: Any,
) -> tuple[None, dict[str, Any]]:
    return None, {
        "error": message,
        "code": code,
        "status": status,
        **details,
    }


def _positive_tenant_id(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _normalize_analytics_scope(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    return _ANALYTICS_SCOPE_ALIASES.get(raw)


def _tenant_owner_for_analytics_scope(
    tenant: TenantProfile,
    scope: str | None,
) -> tuple[int | None, dict[str, Any] | None]:
    declared_scope = _normalize_analytics_scope(getattr(tenant, "tipo", None))
    if (
        scope in {"municipio", "pyme"}
        and declared_scope in {"municipio", "pyme"}
        and declared_scope != scope
    ):
        return None, {
            "code": "tenant_scope_incompatible",
            "error": "El tipo declarado del tenant no es compatible con el scope solicitado.",
            "status": 400,
            "tenant_profile_id": int(tenant.id),
            "tenant_type": getattr(tenant, "tipo", None),
            "scope": scope,
        }
    if scope == "municipio":
        owner_candidates = [getattr(tenant, "municipio_id", None)]
    elif scope == "pyme":
        owner_candidates = [getattr(tenant, "pyme_id", None)]
    else:
        owner_candidates = [
            getattr(tenant, "municipio_id", None),
            getattr(tenant, "pyme_id", None),
        ]

    owners = tuple(
        dict.fromkeys(
            parsed
            for parsed in (_positive_tenant_id(value) for value in owner_candidates)
            if parsed is not None
        )
    )
    if len(owners) != 1:
        return None, {
            "code": "tenant_scope_incompatible" if scope in {"municipio", "pyme"} else "tenant_owner_ambiguous",
            "error": "El tenant no tiene un propietario unico compatible con el scope solicitado.",
            "status": 400,
            "tenant_profile_id": int(tenant.id),
            "scope": scope,
        }

    owner_id = owners[0]
    try:
        owner_resolution = resolve_unique_tenant_for_owner(owner_id)
    except (TicketTenantScopeError, SQLAlchemyError):
        current_app.logger.exception(
            "[analytics] authoritative owner resolution failed owner_id=%s tenant_profile_id=%s",
            owner_id,
            getattr(tenant, "id", None),
        )
        return None, {
            "code": "tenant_resolution_unavailable",
            "error": "No se pudo validar el propietario del tenant de analytics.",
            "status": 503,
            "tenant_profile_id": int(tenant.id),
            "owner_tenant_id": owner_id,
        }

    if (
        owner_resolution.status != "unique"
        or owner_resolution.tenant is None
        or int(owner_resolution.tenant.id) != int(tenant.id)
    ):
        return None, {
            "code": f"tenant_owner_{owner_resolution.status}",
            "error": "El propietario legacy no identifica un unico tenant de analytics.",
            "status": 409,
            "tenant_profile_id": int(tenant.id),
            "owner_tenant_id": owner_id,
            "candidate_ids": list(owner_resolution.candidate_ids),
        }
    return owner_id, None


def _tenant_profile_by_slug(slug: str) -> TenantProfile | None:
    return TenantProfile.query.filter(TenantProfile.slug.ilike(slug)).one_or_none()


def _tenant_from_generic_numeric_hint(
    numeric_id: int,
) -> tuple[TenantProfile | None, str | None, dict[str, Any] | None]:
    """Resolve the legacy ``tenant_id`` dual namespace without guessing.

    Older analytics clients sent the owner user id while newer surfaces often
    sent ``TenantProfile.id``. Both remain readable only when they identify the
    same profile or exactly one namespace has a match. Numeric collisions fail
    closed; callers can disambiguate with ``tenant_profile_id`` or a slug.
    """

    exact_profile = db.session.get(TenantProfile, numeric_id)
    try:
        owner_resolution = resolve_unique_tenant_for_owner(numeric_id)
    except (TicketTenantScopeError, SQLAlchemyError):
        current_app.logger.exception(
            "[analytics] numeric tenant resolution failed tenant_id=%s",
            numeric_id,
        )
        return None, None, {
            "code": "tenant_resolution_unavailable",
            "error": "No se pudo validar el tenant de analytics.",
            "status": 503,
            "tenant_id": numeric_id,
        }

    if owner_resolution.status == "ambiguous":
        return None, None, {
            "code": "tenant_owner_ambiguous",
            "error": "tenant_id coincide con un propietario asociado a varios tenants.",
            "status": 409,
            "tenant_id": numeric_id,
            "candidate_ids": list(owner_resolution.candidate_ids),
        }

    owner_profile = owner_resolution.tenant if owner_resolution.status == "unique" else None
    if exact_profile is not None and owner_profile is not None and int(exact_profile.id) != int(owner_profile.id):
        return None, None, {
            "code": "tenant_numeric_namespace_ambiguous",
            "error": "tenant_id coincide con perfiles distintos en los namespaces profile y owner.",
            "status": 409,
            "tenant_id": numeric_id,
            "candidate_ids": [int(exact_profile.id), int(owner_profile.id)],
        }
    if exact_profile is not None:
        source = "tenant_id_profile" if owner_profile is None else "tenant_id_profile_and_owner"
        return exact_profile, source, None
    if owner_profile is not None:
        return owner_profile, "tenant_id_owner", None
    return None, None, {
        "code": "tenant_not_found",
        "error": "tenant_id no corresponde a un TenantProfile ni a un propietario unico.",
        "status": 404,
        "tenant_id": numeric_id,
    }


def _resolve_authoritative_analytics_tenant(
    *,
    tenant_profile_id: Any = None,
    tenant_slug: Any = None,
    tenant_id: Any = None,
    owner_tenant_id: Any = None,
    scope: Any = None,
) -> tuple[int | None, dict[str, Any]]:
    raw_scope = str(scope or "").strip().lower()
    normalized_scope = _normalize_analytics_scope(raw_scope)
    if raw_scope and normalized_scope is None:
        return _analytics_tenant_resolution_error(
            "tenant_scope_invalid",
            f"Scope de analytics no soportado: {raw_scope}",
            scope=raw_scope,
        )

    candidates: list[tuple[TenantProfile, str]] = []

    if tenant_profile_id not in (None, ""):
        profile_id = _positive_tenant_id(tenant_profile_id)
        if profile_id is None:
            return _analytics_tenant_resolution_error(
                "tenant_profile_id_invalid",
                "tenant_profile_id debe ser un entero positivo.",
            )
        tenant = db.session.get(TenantProfile, profile_id)
        if tenant is None:
            return _analytics_tenant_resolution_error(
                "tenant_profile_not_found",
                "tenant_profile_id no corresponde a un tenant existente.",
                status=404,
                tenant_profile_id=profile_id,
            )
        candidates.append((tenant, "tenant_profile_id"))

    normalized_slug = str(tenant_slug or "").strip().lower()
    if normalized_slug:
        try:
            tenant = _tenant_profile_by_slug(normalized_slug)
        except SQLAlchemyError:
            current_app.logger.exception(
                "[analytics] tenant slug resolution failed slug=%s",
                normalized_slug,
            )
            return _analytics_tenant_resolution_error(
                "tenant_resolution_unavailable",
                "No se pudo resolver el tenant de analytics.",
                status=503,
                tenant_slug=normalized_slug,
            )
        if tenant is None:
            return _analytics_tenant_resolution_error(
                "tenant_slug_not_found",
                f"tenant_slug '{normalized_slug}' no encontrado",
                status=404,
                tenant_slug=normalized_slug,
            )
        candidates.append((tenant, "tenant_slug"))

    if owner_tenant_id not in (None, ""):
        owner_id = _positive_tenant_id(owner_tenant_id)
        if owner_id is None:
            return _analytics_tenant_resolution_error(
                "owner_tenant_id_invalid",
                "owner_tenant_id debe ser un entero positivo.",
            )
        try:
            owner_resolution = resolve_unique_tenant_for_owner(owner_id)
        except (TicketTenantScopeError, SQLAlchemyError):
            current_app.logger.exception(
                "[analytics] explicit owner resolution failed owner_id=%s",
                owner_id,
            )
            return _analytics_tenant_resolution_error(
                "tenant_resolution_unavailable",
                "No se pudo validar el propietario del tenant de analytics.",
                status=503,
                owner_tenant_id=owner_id,
            )
        if owner_resolution.status != "unique" or owner_resolution.tenant is None:
            return _analytics_tenant_resolution_error(
                f"tenant_owner_{owner_resolution.status}",
                "owner_tenant_id no identifica un unico tenant de analytics.",
                status=409 if owner_resolution.status == "ambiguous" else 404,
                owner_tenant_id=owner_id,
                candidate_ids=list(owner_resolution.candidate_ids),
            )
        candidates.append((owner_resolution.tenant, "owner_tenant_id"))

    if tenant_id not in (None, ""):
        matching_candidate = next(
            (
                c
                for c, _ in candidates
                if str(c.id) == str(tenant_id)
                or str(getattr(c, "municipio_id", "") or "") == str(tenant_id)
                or str(getattr(c, "pyme_id", "") or "") == str(tenant_id)
            ),
            None,
        )
        if matching_candidate is None:
            numeric_id = _positive_tenant_id(tenant_id)
            if numeric_id is None:
                return _analytics_tenant_resolution_error(
                    "tenant_id_invalid",
                    "tenant_id debe ser un entero positivo o debe enviarse un tenant_slug.",
                )
            tenant, source, error = _tenant_from_generic_numeric_hint(numeric_id)
            if error is not None:
                return None, error
            assert tenant is not None and source is not None
            candidates.append((tenant, source))

    if not candidates:
        return _analytics_tenant_resolution_error(
            "tenant_unresolved",
            "No se recibio un identificador de tenant valido.",
        )

    candidate_ids = {int(candidate.id) for candidate, _source in candidates}
    if len(candidate_ids) != 1:
        return _analytics_tenant_resolution_error(
            "tenant_context_inconsistent",
            "Los identificadores recibidos corresponden a tenants distintos.",
            status=409,
            candidate_ids=sorted(candidate_ids),
        )

    tenant = candidates[0][0]
    owner_id, owner_error = _tenant_owner_for_analytics_scope(tenant, normalized_scope)
    if owner_error is not None:
        return None, owner_error
    assert owner_id is not None

    return int(tenant.id), {
        "tenant_profile_id": int(tenant.id),
        "owner_tenant_id": owner_id,
        "tenant_slug": tenant.slug,
        "tenant_type": getattr(tenant, "tipo", None),
        "scope": normalized_scope,
        "resolution_sources": list(dict.fromkeys(source for _tenant, source in candidates)),
    }


def _resolve_identity_event_tenant_id(filters: AnalyticsFilters) -> tuple[int | None, dict[str, Any]]:
    """Resolve the authoritative TenantProfile key used by the event store."""

    return _resolve_authoritative_analytics_tenant(
        tenant_profile_id=getattr(filters, "tenant_profile_id", None),
        tenant_slug=_requested_tenant_slug(),
        tenant_id=getattr(filters, "tenant_id", None),
        scope=getattr(filters, "scope", None),
    )

def _consistent_request_hint(values: list[Any], *, numeric: bool) -> tuple[Any, bool]:
    normalized: list[Any] = []
    for value in values:
        if value in (None, ""):
            continue
        if numeric:
            parsed = _positive_tenant_id(value)
            if parsed is None:
                return None, False
            normalized.append(parsed)
        else:
            normalized.append(str(value).strip().lower())
    unique = list(dict.fromkeys(normalized))
    if len(unique) > 1:
        return None, False
    return (unique[0] if unique else None), True


def _resolve_tenant_id_from_event_payload(payload: dict) -> tuple[int | None, dict[str, Any]]:
    """Normalize event tenant hints to the authoritative TenantProfile id.

    ``tenant_id`` remains a dual-namespace compatibility input (profile id or
    legacy owner id), but collisions are rejected. New clients should send
    ``tenant_profile_id`` or ``tenant_slug`` explicitly.
    """

    raw_tenant_values = [
        payload.get("tenant_id"),
        request.args.get("tenant_id"),
        payload.get("tenant"),
        request.args.get("tenant"),
    ]
    numeric_tenant_values: list[Any] = []
    slug_values: list[Any] = [payload.get("tenant_slug"), request.args.get("tenant_slug")]
    for value in raw_tenant_values:
        if value in (None, ""):
            continue
        if _positive_tenant_id(value) is not None:
            numeric_tenant_values.append(value)
        else:
            slug_values.append(value)

    profile_id, profile_consistent = _consistent_request_hint(
        [payload.get("tenant_profile_id"), request.args.get("tenant_profile_id")],
        numeric=True,
    )
    owner_id, owner_consistent = _consistent_request_hint(
        [payload.get("owner_tenant_id"), request.args.get("owner_tenant_id")],
        numeric=True,
    )
    tenant_id, tenant_consistent = _consistent_request_hint(numeric_tenant_values, numeric=True)
    tenant_slug, slug_consistent = _consistent_request_hint(slug_values, numeric=False)
    if not all((profile_consistent, owner_consistent, tenant_consistent, slug_consistent)):
        return _analytics_tenant_resolution_error(
            "tenant_context_inconsistent",
            "Los identificadores de tenant del body y la URL son invalidos o inconsistentes.",
            status=409,
        )

    scope_values = [payload.get("scope"), request.args.get("scope"), request.args.get("entity")]
    normalized_scopes = [
        normalized
        for normalized in (_normalize_analytics_scope(value) for value in scope_values if value not in (None, ""))
        if normalized is not None
    ]
    raw_scopes = [str(value).strip() for value in scope_values if value not in (None, "")]
    if raw_scopes and len(normalized_scopes) != len(raw_scopes):
        return _analytics_tenant_resolution_error(
            "tenant_scope_invalid",
            "Se recibio un scope de tenant no soportado.",
        )
    if len(set(normalized_scopes)) > 1:
        return _analytics_tenant_resolution_error(
            "tenant_scope_inconsistent",
            "Los scopes de tenant del body y la URL son inconsistentes.",
            status=409,
        )

    tenant_type, tenant_type_consistent = _consistent_request_hint(
        [payload.get("tenant_type"), request.args.get("tenant_type")],
        numeric=False,
    )
    if not tenant_type_consistent:
        return _analytics_tenant_resolution_error(
            "tenant_type_inconsistent",
            "Los tipos de tenant del body y la URL son inconsistentes.",
            status=409,
        )
    tenant_type_scope = _normalize_analytics_scope(tenant_type)
    explicit_scope = normalized_scopes[0] if normalized_scopes else None
    if explicit_scope and tenant_type_scope and explicit_scope != tenant_type_scope:
        return _analytics_tenant_resolution_error(
            "tenant_scope_inconsistent",
            "tenant_type y scope corresponden a tipos de tenant distintos.",
            status=409,
        )

    profile_id, resolution = _resolve_authoritative_analytics_tenant(
        tenant_profile_id=profile_id,
        tenant_slug=tenant_slug,
        tenant_id=tenant_id,
        owner_tenant_id=owner_id,
        scope=explicit_scope or tenant_type_scope,
    )
    if profile_id is None:
        return None, resolution

    # Vertical tenant types such as ``colegio`` use the PyME owner column but
    # remain first-class classifications. Validate them against the profile
    # instead of rejecting them as unknown analytics scopes.
    if tenant_type and tenant_type_scope is None:
        actual_tenant_type = str(resolution.get("tenant_type") or "").strip().lower()
        if actual_tenant_type != tenant_type:
            return _analytics_tenant_resolution_error(
                "tenant_type_incompatible",
                "tenant_type no coincide con el tipo del TenantProfile resuelto.",
                status=409,
                tenant_profile_id=profile_id,
                tenant_type=tenant_type,
                actual_tenant_type=actual_tenant_type,
            )
    return profile_id, resolution




def _current_contact_identity() -> dict:
    identity = getattr(g, "contact_identity", None)
    if isinstance(identity, dict):
        return identity
    return {}


def _build_event_payload_with_identity(payload: dict) -> dict:
    event_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    enriched_payload = dict(event_payload)

    identity = _current_contact_identity()
    if identity:
        def _is_missing(value) -> bool:
            if value is None:
                return True
            if isinstance(value, str) and not value.strip():
                return True
            return False

        for key, source_key in (
            ("contact_key", "contact_key"),
            ("conversation_id", "conversation_id"),
            ("phone_e164", "phone_e164"),
            ("identity_source", "source"),
        ):
            if _is_missing(enriched_payload.get(key)) and identity.get(source_key) is not None:
                enriched_payload[key] = identity.get(source_key)

    # Also honor direct API payload hints when provided by trusted callers.
    if payload.get("contact_key"):
        enriched_payload["contact_key"] = payload.get("contact_key")
    if payload.get("conversation_id"):
        enriched_payload["conversation_id"] = payload.get("conversation_id")

    return {k: v for k, v in enriched_payload.items() if v is not None}

def _resolve_event_name(payload: dict) -> str:
    for key in ("event_name", "event", "name", "type"):
        value = payload.get(key)
        if value is None:
            value = request.args.get(key)
        text = str(value or "").strip()
        if text:
            return text
    # Keep ingest resilient for frontend telemetry beacons that omit the event
    # name while still sending tenant context.
    return "frontend_analytics_event"

def _json_response(payload, status: int = 200, request_id: str | None = None):
    resolved_request_id = request_id
    if isinstance(resolved_request_id, str):
        resolved_request_id = resolved_request_id.strip() or None
    if not resolved_request_id:
        header_request_id = request.headers.get("X-Request-Id")
        if isinstance(header_request_id, str):
            cleaned = header_request_id.strip()
            if cleaned:
                resolved_request_id = cleaned
    if not resolved_request_id:
        resolved_request_id = uuid.uuid4().hex
    needs_request_id = (
        isinstance(payload, dict)
        and (
            payload.get("request_id") is None
            or (isinstance(payload.get("request_id"), str) and not payload.get("request_id").strip())
        )
    )
    if needs_request_id:
        payload = {**payload, "request_id": resolved_request_id}
    response = jsonify(payload)
    response.status_code = status
    response.headers.setdefault("X-Request-Id", resolved_request_id)
    return response


def legacy_tenant_wide_analytics_admin_only(fn):
    """Fail closed before route-local analytics scope parsing/materialization."""

    @wraps(fn)
    def _wrapped(*args, **kwargs):
        request_id = (request.headers.get("X-Request-Id") or "").strip() or uuid.uuid4().hex
        denial = legacy_tenant_wide_analytics_denial(request_id=request_id)
        if denial is not None:
            response = _json_response(denial, status=403, request_id=request_id)
            response.headers["Cache-Control"] = "no-store"
            return response
        return fn(*args, **kwargs)

    return _wrapped


def _analytics_event_ignored_response(
    reason: str,
    *,
    tenant_id: int | None = None,
    event_name: str | None = None,
    status: int = 202,
):
    return _json_response(
        {
            "ok": True,
            "success": True,
            "accepted": False,
            "ignored": True,
            "reason": reason,
            "contract_version": ANALYTICS_EVENT_INGEST_CONTRACT_VERSION,
            "tenant_id": tenant_id,
            "event_name": event_name,
        },
        status=status,
    )


def _error_response(message: str, *, status: int, capability: str | None = None):
    payload: dict[str, Any] = {
        "error": {
            "code": status,
            "message": message,
        }
    }
    if capability:
        payload["error"]["capability"] = capability
    return _json_response(payload, status=status)


def _event_has_contact_identity(metadata: dict[str, Any] | None, session_id: str | None, anon_id: str | None) -> bool:
    if isinstance(metadata, dict):
        if metadata.get("contact_key") or metadata.get("conversation_id"):
            return True

    return bool(session_id or anon_id)


def _geo_int(value: Any, default: int = 0) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return default


def _geo_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _geo_risk_level(intensity: float) -> dict[str, Any]:
    if intensity >= 0.85:
        return {"level": "critical", "label": "Critico", "priority": 4, "tone": "red"}
    if intensity >= 0.6:
        return {"level": "high", "label": "Alto", "priority": 3, "tone": "amber"}
    if intensity >= 0.3:
        return {"level": "medium", "label": "Medio", "priority": 2, "tone": "violet"}
    return {"level": "low", "label": "Bajo", "priority": 1, "tone": "cyan"}


def _dominant_geo_category(categories: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(categories, dict) or not categories:
        return {"category": "sin_dato", "count": 0}
    rows = []
    for category, count in categories.items():
        value = _geo_int(count)
        if value <= 0:
            continue
        rows.append({"category": str(category or "sin_dato").strip() or "sin_dato", "count": value})
    if not rows:
        return {"category": "sin_dato", "count": 0}
    rows.sort(key=lambda item: item["count"], reverse=True)
    return rows[0]


def _geo_cell_count(cell: dict[str, Any]) -> int:
    count = _geo_int(cell.get("count"))
    if count > 0:
        return count
    categories = cell.get("categories") if isinstance(cell, dict) else None
    if isinstance(categories, dict):
        category_total = sum(_geo_int(value) for value in categories.values())
        if category_total > 0:
            return category_total
    return max(count, 0)


def _heatmap_cell_for_frontend(cell: dict[str, Any], *, max_count: int) -> dict[str, Any]:
    enriched = dict(cell)
    count = _geo_cell_count(enriched)
    fallback_intensity = (count / max_count) if max_count else 0.0
    intensity = _geo_float(enriched.get("intensity"), fallback_intensity)
    intensity = max(0.0, min(1.0, round(intensity, 4)))
    dominant = _dominant_geo_category(enriched.get("categories"))
    risk = _geo_risk_level(intensity)
    enriched["count"] = count
    enriched["intensity"] = intensity
    enriched["dominant_category"] = dominant["category"]
    enriched["dominant_category_count"] = dominant["count"]
    enriched["risk"] = risk
    enriched["visual"] = {
        "radius_px": 16 + int(34 * intensity),
        "glow_opacity": round(0.25 + (0.55 * intensity), 2),
        "pulse": risk["priority"] >= 3,
        "label_mode": "always" if risk["priority"] >= 3 else "hover",
        "z_index": 10 + risk["priority"],
    }
    enriched["operator_context"] = {
        "summary": f"{count} casos en zona; foco {dominant['category']}",
        "recommended_action": "prioritize_dispatch" if risk["priority"] >= 3 else "monitor",
        "crm_filter": {
            "category": dominant["category"],
            "bbox_cell": enriched.get("cell_id"),
        },
    }
    return enriched


def _point_for_frontend(point: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(point)
    status = str(enriched.get("estado") or "").strip().lower()
    category = str(enriched.get("categoria") or enriched.get("rubro") or "sin_dato").strip() or "sin_dato"
    high_attention = status in {"nuevo", "abierto", "en_proceso", "pendiente", "alta", "vencido"}
    risk = (
        {"level": "high", "label": "Requiere atencion", "priority": 3, "tone": "amber"}
        if high_attention
        else _geo_risk_level(0.25)
    )
    enriched["categoria"] = category
    enriched["risk"] = risk
    enriched["visual"] = {
        "marker": "pulse" if high_attention else "dot",
        "radius_px": 14 if high_attention else 9,
        "label_mode": "hover",
    }
    return enriched


def _augment_geo_payload_for_frontend(data: dict[str, Any], *, module: str) -> dict[str, Any]:
    """Attach frontend-ready geo layer hints (OSM/OpenStreet + category overlays)."""
    payload = dict(data or {})
    category_totals: dict[str, int] = {}
    bounds = (payload.get("meta") or {}).get("map", {}).get("bounds") if isinstance(payload.get("meta"), dict) else None
    heatmap_cells = list(payload.get("cells") or [])
    point_rows = list(payload.get("points") or [])

    if module == "heatmap":
        max_count = max((_geo_cell_count(cell) for cell in heatmap_cells if isinstance(cell, dict)), default=0)
        enriched_cells = []
        for cell in heatmap_cells:
            if not isinstance(cell, dict):
                continue
            cell = _heatmap_cell_for_frontend(cell, max_count=max_count)
            enriched_cells.append(cell)
            for category, count in (cell.get("categories") or {}).items():
                category_key = str(category or "sin_dato").strip() or "sin_dato"
                try:
                    category_totals[category_key] = category_totals.get(category_key, 0) + int(count or 0)
                except (TypeError, ValueError):
                    continue
        payload["cells"] = enriched_cells
    elif module == "points":
        enriched_points = []
        for point in point_rows:
            if not isinstance(point, dict):
                continue
            point = _point_for_frontend(point)
            enriched_points.append(point)
            category_key = str(point.get("categoria") or "sin_dato").strip() or "sin_dato"
            category_totals[category_key] = category_totals.get(category_key, 0) + 1
        payload["points"] = enriched_points

    top_categories = sorted(
        (
            {"category": category, "count": count}
            for category, count in category_totals.items()
            if count > 0
        ),
        key=lambda item: item["count"],
        reverse=True,
    )
    cells = payload.get("cells") or []
    points = payload.get("points") or []
    total_items = len(cells) if module == "heatmap" else len(points)
    total_cases = sum(_geo_int(cell.get("count")) for cell in cells if isinstance(cell, dict)) if module == "heatmap" else total_items
    top_hotspots = sorted(
        [
            {
                "id": cell.get("cell_id"),
                "lat": cell.get("centroid_lat"),
                "lon": cell.get("centroid_lon"),
                "count": cell.get("count"),
                "intensity": cell.get("intensity"),
                "category": cell.get("dominant_category"),
                "risk": cell.get("risk"),
            }
            for cell in cells
            if isinstance(cell, dict)
        ],
        key=lambda item: (_geo_int(item.get("count")), _geo_float(item.get("intensity"))),
        reverse=True,
    )[:8]
    if module == "points":
        top_hotspots = [
            {
                "id": index + 1,
                "lat": point.get("lat"),
                "lon": point.get("lon"),
                "count": 1,
                "intensity": 1.0,
                "category": point.get("categoria"),
                "risk": point.get("risk"),
            }
            for index, point in enumerate(points[:8])
            if isinstance(point, dict)
        ]

    payload["map_layers"] = {
        "contract_version": ANALYTICS_GEO_LAYERS_CONTRACT_VERSION,
        "provider": {
            "name": "openstreetmap",
            "tiles": [{"url": "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", "attribution": "© OpenStreetMap contributors"}],
            "recommended_engine": "maplibre-gl",
        },
        "viewport": {
            "bounds": bounds,
            "fit_bounds": bool(bounds),
            "padding": 72,
            "fallback_zoom": 12,
        },
        "category_heatmap": {
            "enabled": bool(top_categories),
            "source_module": module,
            "top_categories": top_categories[:12],
            "available_categories": [item["category"] for item in top_categories],
            "supports_multi_select": True,
            "bounds": bounds,
        },
        "intensity": {
            "metric": "case_count" if module == "heatmap" else "point_count",
            "total_items": total_items,
            "total_cases": total_cases,
            "scale": [
                {"stop": 0.0, "color": "#22d3ee", "label": "bajo"},
                {"stop": 0.35, "color": "#8b5cf6", "label": "medio"},
                {"stop": 0.65, "color": "#f59e0b", "label": "alto"},
                {"stop": 0.9, "color": "#ef4444", "label": "critico"},
            ],
        },
        "hotspots": {
            "enabled": bool(top_hotspots),
            "top": top_hotspots,
            "focus": top_hotspots[0] if top_hotspots else None,
            "suggested_crm_action": "open_filtered_ticket_inbox" if top_hotspots else "collect_more_geo_data",
        },
        "visual_system": {
            "style": "premium_operational_map",
            "renderer": "webgl_heatmap" if module == "heatmap" else "clustered_points",
            "animations": {
                "radar_sweep": True,
                "pulse_hotspots": True,
                "smooth_zoom": True,
                "live_beacon": module == "points",
            },
            "legend_position": "bottom-left",
            "panel_density": "crm_dense",
        },
        "operator_metrics": {
            "total_cases": total_cases,
            "visible_layers": total_items,
            "top_category": top_categories[0]["category"] if top_categories else None,
            "critical_hotspots": sum(
                1 for item in top_hotspots if ((item.get("risk") or {}).get("level") == "critical")
            ),
            "recommended_next_step": "prioritize_top_hotspot" if top_hotspots else "request_location_capture",
        },
    }
    render_contract = payload.setdefault("render_contract", {})
    if isinstance(render_contract, dict):
        render_contract.setdefault("recommended_component", "PremiumTerritoryMap")
        render_contract.setdefault("interaction_model", "filterable_operational_heatmap")
        render_contract.setdefault("crm_deep_link", "/perfil?tab=tickets")
        render_contract.setdefault("supports_live_refresh", True)
    return payload


def _requested_categories() -> list[str]:
    raw_values: list[str] = []
    raw_values.extend(request.args.getlist("category"))
    raw_values.extend(request.args.getlist("categories"))
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        for chunk in str(raw_value or "").split(","):
            value = chunk.strip().lower()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
    return normalized


def _apply_geo_category_filter(data: dict[str, Any], *, module: str, categories: list[str]) -> dict[str, Any]:
    if not categories:
        return data

    payload = dict(data or {})
    wanted = {category.lower() for category in categories}

    if module == "heatmap":
        filtered_cells: list[dict[str, Any]] = []
        for cell in payload.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            original_categories = cell.get("categories") or {}
            selected_categories = {
                str(category): count
                for category, count in original_categories.items()
                if str(category or "").strip().lower() in wanted
            }
            if not selected_categories:
                continue
            try:
                selected_count = int(sum(int(value or 0) for value in selected_categories.values()))
            except (TypeError, ValueError):
                continue
            new_cell = dict(cell)
            new_cell["categories"] = selected_categories
            new_cell["count"] = selected_count
            filtered_cells.append(new_cell)

        max_count = max((int(cell.get("count") or 0) for cell in filtered_cells), default=0)
        for cell in filtered_cells:
            count = int(cell.get("count") or 0)
            cell["intensity"] = round((count / max_count), 4) if max_count else 0.0
        payload["cells"] = filtered_cells

    elif module == "points":
        payload["points"] = [
            point
            for point in (payload.get("points") or [])
            if isinstance(point, dict) and str(point.get("categoria") or "").strip().lower() in wanted
        ]

    return payload






def _parse_channel_targets(raw_value: Any, default_target: float) -> dict[str, float]:
    if raw_value is None:
        return {}

    parsed: dict[str, Any]
    if isinstance(raw_value, dict):
        parsed = raw_value
    else:
        text = str(raw_value).strip()
        if not text:
            return {}
        try:
            loaded = json.loads(text)
            parsed = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            parsed = {}
            for chunk in text.split(","):
                if ":" not in chunk:
                    continue
                channel, value = chunk.split(":", 1)
                parsed[channel.strip()] = value.strip()

    targets: dict[str, float] = {}
    for channel, value in parsed.items():
        channel_key = str(channel or "").strip().lower()
        if not channel_key:
            continue
        try:
            targets[channel_key] = float(value)
        except (TypeError, ValueError):
            targets[channel_key] = default_target
    return targets

def _coverage_slo_status(coverage_pct: float, target_pct: float) -> str:
    try:
        target = float(target_pct)
    except (TypeError, ValueError):
        target = 90.0
    return "ok" if float(coverage_pct) >= target else "below_target"


def _parse_positive_int_arg(name: str, *, default: int, minimum: int = 1, maximum: int | None = None) -> int:
    raw_value = request.args.get(name, default)
    try:
        parsed = int(str(raw_value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{name} debe ser numérico")
    if parsed < minimum:
        raise ValueError(f"{name} debe ser >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise ValueError(f"{name} debe ser <= {maximum}")
    return parsed



def _build_identity_alerts(
    channels: dict[str, Any],
    target_pct: float,
    *,
    channel_targets: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    channel_targets = channel_targets or {}

    for channel, stats in (channels or {}).items():
        normalized_channel = str(channel or "").strip().lower() or "unknown"
        channel_target = float(channel_targets.get(normalized_channel, target_pct))
        try:
            coverage = float(stats.get("coverage_pct", 0.0))
        except (TypeError, ValueError):
            coverage = 0.0
        if coverage >= channel_target:
            continue

        alerts.append(
            {
                "type": "identity_coverage_below_target",
                "channel": normalized_channel,
                "coverage_pct": coverage,
                "target_pct": channel_target,
                "gap_pct": round(channel_target - coverage, 2),
                "recommended_action": "increase_contact_key_propagation",
            }
        )
    return alerts



def _build_identity_alert_event_payloads(
    *,
    tenant_id: int,
    alerts: list[dict[str, Any]],
    target_pct: float,
    overall_coverage_pct: float,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for alert in alerts:
        payloads.append(
            {
                "tenant_id": tenant_id,
                "event_name": "identity_coverage_alert",
                "channel": alert.get("channel"),
                "payload": {
                    "type": alert.get("type"),
                    "coverage_pct": alert.get("coverage_pct"),
                    "target_pct": alert.get("target_pct", target_pct),
                    "gap_pct": alert.get("gap_pct"),
                    "recommended_action": alert.get("recommended_action"),
                    "overall_coverage_pct": overall_coverage_pct,
                },
            }
        )
    return payloads

def _compute_identity_coverage(events: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(events)
    if total == 0:
        return {
            "total_events": 0,
            "events_with_identity": 0,
            "coverage_pct": 0.0,
            "channels": {},
        }

    events_with_identity = 0
    per_channel: dict[str, dict[str, int]] = {}

    for event in events:
        channel = str(event.get("channel") or "unknown").strip().lower() or "unknown"
        channel_stats = per_channel.setdefault(channel, {"total": 0, "with_identity": 0})
        channel_stats["total"] += 1

        has_identity = _event_has_contact_identity(
            event.get("metadata") if isinstance(event.get("metadata"), dict) else None,
            event.get("session_id"),
            event.get("anon_id"),
        )
        if has_identity:
            events_with_identity += 1
            channel_stats["with_identity"] += 1

    channels_payload = {}
    for channel, stats in per_channel.items():
        channel_total = stats.get("total", 0)
        channel_with_identity = stats.get("with_identity", 0)
        channels_payload[channel] = {
            "total": channel_total,
            "with_identity": channel_with_identity,
            "coverage_pct": round((channel_with_identity / channel_total) * 100.0, 2) if channel_total else 0.0,
        }

    return {
        "total_events": total,
        "events_with_identity": events_with_identity,
        "coverage_pct": round((events_with_identity / total) * 100.0, 2),
        "channels": channels_payload,
    }


@analytics_bp.route("/summary", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_summary():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    current_app.logger.info("[analytics] summary %s", filters)
    data = get_summary(filters)
    return _json_response(data)


@analytics_bp.route("/timeseries", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_timeseries():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    metric = request.args.get("metric", "tickets")
    group = request.args.get("group")
    data = get_timeseries(filters, metric=metric, group=group)
    return _json_response(data)


@analytics_bp.route("/breakdown", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_breakdown():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    dimension = request.args.get("dimension", "categoria")
    data = get_breakdown(filters, dimension=dimension)
    return _json_response(data)


@analytics_bp.route("/geo/heatmap", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_heatmap():
    request_started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id")
    if isinstance(request_id, str):
        request_id = request_id.strip() or None
    if not request_id:
        request_id = uuid.uuid4().hex
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    categories = _requested_categories()
    data = _apply_geo_category_filter(get_geo_heatmap(filters), module="heatmap", categories=categories)
    data = _augment_geo_payload_for_frontend(data, module="heatmap")
    if categories:
        category_layer = data["map_layers"]["category_heatmap"]
        available_categories = {
            str(value).strip().lower()
            for value in (category_layer.get("available_categories") or [])
            if str(value).strip()
        }
        missing_categories = [value for value in categories if value not in available_categories]
        category_layer["applied_categories"] = categories
        if missing_categories:
            category_layer["missing_categories"] = missing_categories
            category_layer["warning"] = "requested_categories_without_data"
    response = _json_response(data, request_id=request_id)
    elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
    response.headers.setdefault("X-Request-Id", request_id)
    response.headers.setdefault("Server-Timing", f"analytics_geo_heatmap;dur={elapsed_ms}")
    current_app.logger.info(
        "[analytics.geo.heatmap] request_id=%s tenant_id=%s scope=%s cells=%s state=%s total_ms=%s",
        request_id,
        filters.tenant_id,
        filters.scope,
        len(data.get("cells") or []),
        (data.get("render_contract") or {}).get("state"),
        elapsed_ms,
    )
    return response


@analytics_bp.route("/geo/points", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_points():
    request_started = time.perf_counter()
    request_id = request.headers.get("X-Request-Id")
    if isinstance(request_id, str):
        request_id = request_id.strip() or None
    if not request_id:
        request_id = uuid.uuid4().hex
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    try:
        limit = _parse_positive_int_arg("limit", default=500, minimum=1, maximum=5000)
    except ValueError as exc:
        return _error_response(str(exc), status=400)
    categories = _requested_categories()
    data = _apply_geo_category_filter(get_geo_points(filters, limit=limit), module="points", categories=categories)
    data = _augment_geo_payload_for_frontend(data, module="points")
    if categories:
        category_layer = data["map_layers"]["category_heatmap"]
        available_categories = {
            str(value).strip().lower()
            for value in (category_layer.get("available_categories") or [])
            if str(value).strip()
        }
        missing_categories = [value for value in categories if value not in available_categories]
        category_layer["applied_categories"] = categories
        if missing_categories:
            category_layer["missing_categories"] = missing_categories
            category_layer["warning"] = "requested_categories_without_data"
    response = _json_response(data, request_id=request_id)
    elapsed_ms = round((time.perf_counter() - request_started) * 1000.0, 2)
    response.headers.setdefault("X-Request-Id", request_id)
    response.headers.setdefault("Server-Timing", f"analytics_geo_points;dur={elapsed_ms}")
    current_app.logger.info(
        "[analytics.geo.points] request_id=%s tenant_id=%s scope=%s points=%s state=%s total_ms=%s",
        request_id,
        filters.tenant_id,
        filters.scope,
        len(data.get("points") or []),
        (data.get("render_contract") or {}).get("state"),
        elapsed_ms,
    )
    return response


@analytics_bp.route("/top", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_top():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    category = request.args.get("category", "barrios")
    limit = int(request.args.get("limit", 10))
    data = get_top(filters, category=category, limit=limit)
    return _json_response(data)


@analytics_bp.route("/operations", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_operations():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    data = get_operations_overview(filters)
    return _json_response(data)


@analytics_bp.route("/cohorts", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_cohorts():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "visor", required_capability="analytics.read")
    data = get_cohorts(filters)
    return _json_response(data)


@analytics_bp.route("/whatsapp/templates", methods=["GET"])
@legacy_tenant_wide_analytics_admin_only
def analytics_templates():
    filters = parse_filters(request.args)
    require_access(filters.tenant_id, "operador", required_capability="analytics.admin")
    data = get_whatsapp_templates(filters)
    return _json_response(data)




@analytics_bp.route("/identity/coverage", methods=["GET"])
def analytics_identity_coverage():
    filters = parse_filters(request.args)
    event_tenant_id, tenant_resolution = _resolve_identity_event_tenant_id(filters)
    if event_tenant_id is None:
        return _error_response(
            tenant_resolution.get("error", "tenant no resuelto"),
            status=int(tenant_resolution.get("status") or 400),
        )
    access_tenant_id = str(tenant_resolution["owner_tenant_id"])
    require_access(access_tenant_id, "visor", required_capability="analytics.read")

    try:
        limit = int(request.args.get("limit", 5000))
    except (TypeError, ValueError):
        return _error_response("limit debe ser numérico", status=400)
    if limit < 1:
        limit = 1
    if limit > 20000:
        limit = 20000

    data_status = "ok"
    warnings: list[dict[str, Any]] = []
    try:
        query = AnalyticsEventV2.query.filter(AnalyticsEventV2.tenant_id == event_tenant_id)
        if filters.date_from:
            query = query.filter(AnalyticsEventV2.ts >= filters.date_from)
        if filters.date_to:
            query = query.filter(AnalyticsEventV2.ts <= filters.date_to)

        rows = (
            query.with_entities(
                AnalyticsEventV2.channel.label("channel"),
                AnalyticsEventV2.metadata_payload.label("metadata"),
                AnalyticsEventV2.session_id.label("session_id"),
                AnalyticsEventV2.anon_id.label("anon_id"),
            )
            .order_by(AnalyticsEventV2.ts.desc())
            .limit(limit)
            .all()
        )
    except SQLAlchemyError as exc:
        db.session.rollback()
        current_app.logger.exception(
            "[analytics] identity coverage query failed tenant_id=%s filters_tenant=%s",
            event_tenant_id,
            filters.tenant_id,
        )
        rows = []
        data_status = "degraded"
        warnings.append(
            {
                "code": "identity_coverage_query_failed",
                "message": "No se pudo leer la cobertura de identidad; se devuelve una muestra vacia para no bloquear el panel.",
            }
        )

    events = [
        {
            "channel": row.channel,
            "metadata": row.metadata if isinstance(row.metadata, dict) else {},
            "session_id": row.session_id,
            "anon_id": row.anon_id,
        }
        for row in rows
    ]

    coverage = _compute_identity_coverage(events)

    target_pct_raw = request.args.get("target_pct", 90)
    try:
        target_pct = float(target_pct_raw)
    except (TypeError, ValueError):
        target_pct = 90.0

    channel_targets = _parse_channel_targets(request.args.get("target_by_channel"), target_pct)
    slo_status = _coverage_slo_status(coverage.get("coverage_pct", 0.0), target_pct)
    alerts = _build_identity_alerts(
        coverage.get("channels") if isinstance(coverage, dict) else {},
        target_pct,
        channel_targets=channel_targets,
    )

    emit_alert_events = str(request.args.get("emit_alert_events", "0")).strip().lower() in {"1", "true", "yes"}
    alert_event_count = 0
    if emit_alert_events and alerts:
        require_access(access_tenant_id, "operador", required_capability="analytics.admin")
        event_payloads = _build_identity_alert_event_payloads(
            tenant_id=event_tenant_id,
            alerts=alerts,
            target_pct=target_pct,
            overall_coverage_pct=float(coverage.get("coverage_pct", 0.0)),
        )
        for event in event_payloads:
            try:
                analytics_ingestor.track(
                    tenant_id=event["tenant_id"],
                    event_name=event["event_name"],
                    payload=event["payload"],
                    channel=event.get("channel") or "system",
                    session_id="identity_coverage_monitor",
                    tenant_type=filters.scope,
                )
                alert_event_count += 1
            except Exception as exc:  # noqa: BLE001 - alert emission must not break dashboard reads.
                db.session.rollback()
                current_app.logger.exception(
                    "[analytics] identity coverage alert emission failed tenant_id=%s channel=%s",
                    event.get("tenant_id"),
                    event.get("channel"),
                )
                data_status = "degraded"
                warnings.append(
                    {
                        "code": "identity_coverage_alert_emit_failed",
                        "message": "La cobertura se calculo, pero no se pudo registrar el evento de alerta.",
                    }
                )
                break

    coverage.update(
        {
            "tenant_id": filters.tenant_id,
            "event_tenant_id": event_tenant_id,
            "tenant_resolution": tenant_resolution,
            "sample_size": len(events),
            "limit": limit,
            "date_from": filters.date_from.isoformat() if filters.date_from else None,
            "date_to": filters.date_to.isoformat() if filters.date_to else None,
            "target_pct": target_pct,
            "target_by_channel": channel_targets,
            "slo_status": slo_status,
            "alerts": alerts,
            "alert_count": len(alerts),
            "emit_alert_events": emit_alert_events,
            "alert_events_emitted": alert_event_count,
            "data_status": data_status,
            "warnings": warnings,
            "contract_version": ANALYTICS_IDENTITY_COVERAGE_CONTRACT_VERSION,
        }
    )
    return _json_response(coverage)

@analytics_bp.route("/health", methods=["GET"])
def analytics_health():
    latest = (
        db.session.query(AnalyticsModuleStatus)
        .order_by(AnalyticsModuleStatus.snapshot_at.desc())
        .first()
    )
    failed_jobs = int(latest.jobs_failed if latest else 0)
    payload = {
        "contract_version": "analytics.health.v1",
        "status": "degraded" if failed_jobs else "ok",
        "cache": {
            "hits": analytics_cache.stats.hits,
            "misses": analytics_cache.stats.misses,
            "evictions": analytics_cache.stats.evictions,
        },
        "jobs": {
            "pending": latest.jobs_pending if latest else 0,
            "running": latest.jobs_running if latest else 0,
            "failed": latest.jobs_failed if latest else 0,
        },
        "last_snapshot": latest.snapshot_at.isoformat() if latest else None,
        "metadata_redacted": True,
    }
    response = _json_response(payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@analytics_bp.route("/event", methods=["POST"])
def analytics_event_ingest():
    payload = request.get_json(silent=True) or {}
    event_name = _resolve_event_name(payload)
    tenant_profile_id, tenant_resolution = _resolve_tenant_id_from_event_payload(payload)
    if tenant_profile_id is None:
        current_app.logger.info(
            "[analytics] ignored event without authoritative tenant context code=%s",
            tenant_resolution.get("code"),
        )
        return _analytics_event_ignored_response("tenant_unresolved", event_name=event_name)

    owner_tenant_id = int(tenant_resolution["owner_tenant_id"])

    try:
        require_access(str(owner_tenant_id), "operador", required_capability="analytics.admin")
    except HTTPException as exc:
        if exc.code not in {401, 403}:
            raise
        current_app.logger.info(
            "[analytics] ignored event due to access guard owner_tenant_id=%s tenant_profile_id=%s status=%s",
            owner_tenant_id,
            tenant_profile_id,
            exc.code,
        )
        return _analytics_event_ignored_response(
            "access_denied",
            tenant_id=owner_tenant_id,
            event_name=event_name,
        )

    identity = _current_contact_identity()
    payload_with_identity = _build_event_payload_with_identity(payload)

    analytics_ingestor.track(
        tenant_id=tenant_profile_id,
        event_name=event_name,
        payload=payload_with_identity,
        user_id=payload.get("user_id"),
        anon_id=payload.get("anon_id") or identity.get("anon_id"),
        channel=payload.get("channel") or request.args.get("channel"),
        session_id=(
            payload.get("session_id")
            or request.args.get("session_id")
            or identity.get("conversation_id")
            or identity.get("contact_key")
        ),
        lat=payload.get("lat") if payload.get("lat") is not None else request.args.get("lat"),
        lng=payload.get("lng") if payload.get("lng") is not None else request.args.get("lng"),
        entity_ref=payload.get("entity_ref") or request.args.get("entity_ref"),
        tenant_type=payload.get("tenant_type") or request.args.get("tenant_type"),
    )
    return _json_response(
        {
            "ok": True,
            "success": True,
            "accepted": True,
            "ignored": False,
            "contract_version": ANALYTICS_EVENT_INGEST_CONTRACT_VERSION,
            # Keep the legacy owner-facing response field stable while making
            # the event-store key explicit for new callers.
            "tenant_id": owner_tenant_id,
            "tenant_profile_id": tenant_profile_id,
            "tenant_resolution": tenant_resolution,
            "event_name": event_name,
            "contact_key": payload_with_identity.get("contact_key"),
            "conversation_id": payload_with_identity.get("conversation_id"),
            "identity_source": payload_with_identity.get("identity_source"),
        },
        status=202,
    )


@analytics_bp.route("/event/schema", methods=["GET"])
def analytics_event_schema():
    tenant_id_raw = request.args.get("tenant_id")
    try:
        tenant_id = int(str(tenant_id_raw or "").strip())
    except (TypeError, ValueError):
        return _error_response("tenant_id requerido y numérico", status=400)

    require_access(str(tenant_id), "visor", required_capability="analytics.read")
    return _json_response(
        {
            "contract_version": ANALYTICS_EVENT_SCHEMA_CONTRACT_VERSION,
            "tenant_id": tenant_id,
            "required_dimensions": [
                "event_name",
                "channel",
                "tenant_id",
            ],
            "recommended_dimensions": [
                "contact_key",
                "conversation_id",
                "screen_name",
                "category",
                "lat",
                "lng",
            ],
            "canonical_events": ANALYTICS_CANONICAL_EVENT_NAMES,
        }
    )


@analytics_bp.route("/ui", methods=["GET"])
def analytics_ui():
    tenant_id = request.args.get("tenant_id")
    scope = request.args.get("scope", "municipio")
    return render_template("analytics/dashboard.html", tenant_id=tenant_id, scope=scope)


@analytics_bp.route("", methods=["GET"])
def analytics_root():
    return analytics_ui()
