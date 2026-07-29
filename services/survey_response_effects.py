"""Durable post-commit effects for canonical survey responses.

The response writer stages these rows in the *same* database transaction as
``EncRespuesta``.  A worker calls :func:`dispatch_survey_response_effects`
only after that transaction commits.  Staging never commits or rolls back the
caller's transaction.

The dispatcher uses compare-and-swap (CAS) leases rather than relying solely
on ``SELECT .. FOR UPDATE SKIP LOCKED``.  That keeps the claim path usable on
SQLite while retaining correct fencing when multiple PostgreSQL workers race.
Database-backed effects (analytics and rewards) are committed atomically with
the terminal outbox transition.  Realtime delivery is necessarily at-least
once; its stable envelope event id lets consumers deduplicate a retry after an
ambiguous worker crash.
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import and_, case, func, or_, update
from sqlalchemy.exc import IntegrityError

from database import db
from models import (
    EncEncuesta,
    EncRespuesta,
    SurveyResponseEffect,
    TenantProfile,
    User,
)


EFFECT_ANALYTICS = "analytics.v1"
EFFECT_REWARD = "reward.v1"
EFFECT_REALTIME = "realtime.v2"
SUPPORTED_EFFECT_TYPES = frozenset(
    {EFFECT_ANALYTICS, EFFECT_REWARD, EFFECT_REALTIME}
)

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_RETRY_WAIT = "retry_wait"
STATUS_SUCCEEDED = "succeeded"
STATUS_SKIPPED = "skipped"
STATUS_DEAD = "dead"
TERMINAL_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_SKIPPED, STATUS_DEAD})

OUTBOX_CONTRACT_VERSION = "surveys.response_effect.v1"
SUMMARY_CONTRACT_VERSION = "surveys.response_effect_summary.v1"
ANALYTICS_PAYLOAD_CONTRACT = "analytics.survey_response_event.v1"
REWARD_PAYLOAD_CONTRACT = "rewards.survey_response_effect.v1"
REALTIME_PAYLOAD_CONTRACT = "surveys.realtime_effect.v2"

DEFAULT_MAX_ATTEMPTS = 8
LEASE_SECONDS = 120
BASE_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 60 * 60
MAX_DISPATCH_LIMIT = 500

_EVENT_NAMESPACE = uuid.UUID("e29de14f-1ca7-5c3f-8d35-d3af8fa5460b")
_SAFE_ERROR_CODE = re.compile(r"[^a-zA-Z0-9_.:-]+")


@dataclass(frozen=True)
class _EffectOutcome:
    status: str
    result: dict[str, Any]


class _PermanentEffectError(RuntimeError):
    """A corrupt effect cannot become valid merely by retrying it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_utc(value: Optional[datetime]) -> datetime:
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return parsed


def _bounded_limit(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        parsed = 50
    return max(1, min(parsed, MAX_DISPATCH_LIMIT))


def _effect_key(response_id: int, effect_type: str) -> str:
    return f"survey-response:{int(response_id)}:{effect_type}"


def _response_scope(survey_id: int, response_id: int) -> str:
    return f"survey:{int(survey_id)}:response:{int(response_id)}"


def _reward_scope(survey_id: int, user_id: int) -> str:
    return f"survey:{int(survey_id)}:user:{int(user_id)}"


def _reward_idempotency_key(survey_id: int, user_id: int) -> str:
    # Preserve the public reward policy already exercised by the platform:
    # one incentive per authenticated user and survey, even when the response
    # uniqueness policy itself is ``libre``.
    return f"survey_reward:{int(survey_id)}:user:{int(user_id)}"


def _stable_event_id(effect_key: str) -> str:
    return str(uuid.uuid5(_EVENT_NAMESPACE, effect_key))


def _answers_count(
    respuesta: EncRespuesta,
    respuestas_payload: Optional[Sequence[Mapping[str, Any]]],
) -> int:
    if respuestas_payload is not None:
        try:
            return max(0, len(respuestas_payload))
        except TypeError:
            pass
    return len(
        {
            int(detalle.pregunta_id)
            for detalle in (getattr(respuesta, "detalles", None) or [])
            if getattr(detalle, "pregunta_id", None) is not None
        }
    )


def _find_existing_effect(
    *,
    tenant_id: int,
    effect_type: str,
    effect_key: str,
    scope_key: str,
    response_id: int,
    payload: Mapping[str, Any],
) -> Optional[SurveyResponseEffect]:
    by_key = SurveyResponseEffect.query.filter_by(effect_key=effect_key).first()
    if by_key is not None:
        return _validate_existing_effect(
            by_key,
            tenant_id=tenant_id,
            effect_type=effect_type,
            effect_key=effect_key,
            scope_key=scope_key,
            response_id=response_id,
            payload=payload,
            matched_by_scope=False,
        )
    by_scope = SurveyResponseEffect.query.filter_by(
        tenant_id=tenant_id,
        effect_type=effect_type,
        scope_key=scope_key,
    ).first()
    if by_scope is None:
        return None
    return _validate_existing_effect(
        by_scope,
        tenant_id=tenant_id,
        effect_type=effect_type,
        effect_key=effect_key,
        scope_key=scope_key,
        response_id=response_id,
        payload=payload,
        matched_by_scope=True,
    )


def _validate_existing_effect(
    effect: SurveyResponseEffect,
    *,
    tenant_id: int,
    effect_type: str,
    effect_key: str,
    scope_key: str,
    response_id: int,
    payload: Mapping[str, Any],
    matched_by_scope: bool,
) -> SurveyResponseEffect:
    """Fail closed if an idempotency collision does not describe one effect.

    A reward scope intentionally belongs to the first response by that user in
    the survey, so a later response may resolve to a different ``response_id``.
    Every other immutable policy field must still match exactly.
    """

    invariant_matches = (
        int(effect.tenant_id) == int(tenant_id)
        and effect.effect_type == effect_type
        and effect.scope_key == scope_key
    )
    existing_payload = (
        dict(effect.payload_json) if isinstance(effect.payload_json, dict) else {}
    )
    expected_payload = dict(payload)
    if matched_by_scope and effect_type == EFFECT_REWARD:
        existing_payload.pop("response_id", None)
        expected_payload.pop("response_id", None)
        payload_matches = existing_payload == expected_payload
        source_matches = int(effect.survey_id) == int(
            payload.get("survey_id") or 0
        )
    else:
        payload_matches = existing_payload == expected_payload
        source_matches = (
            int(effect.response_id) == int(response_id)
            and effect.effect_key == effect_key
        )
    if not invariant_matches or not source_matches or not payload_matches:
        raise RuntimeError("survey_effect_idempotency_collision_mismatch")
    return effect


def _ensure_physical_outer_transaction_for_savepoint() -> None:
    """Keep a SQLite SAVEPOINT subordinate to the caller's transaction.

    Python's sqlite driver does not necessarily emit ``BEGIN`` for the reads
    performed by the idempotency probe.  Without a physical outer transaction,
    releasing the first SAVEPOINT can make staged rows survive a later caller
    rollback.  PostgreSQL already has the desired transaction semantics.
    """

    connection = db.session.connection()
    if connection.dialect.name != "sqlite":
        return
    connection_fairy = connection.connection
    driver_connection = getattr(
        connection_fairy,
        "driver_connection",
        connection_fairy,
    )
    if getattr(driver_connection, "in_transaction", False):
        return
    connection.exec_driver_sql("BEGIN IMMEDIATE")


def _stage_one(
    *,
    tenant_id: int,
    survey_id: int,
    response_id: int,
    effect_type: str,
    scope_key: str,
    payload: Mapping[str, Any],
    now: datetime,
    max_attempts: int,
) -> SurveyResponseEffect:
    effect_key = _effect_key(response_id, effect_type)
    existing = _find_existing_effect(
        tenant_id=tenant_id,
        effect_type=effect_type,
        effect_key=effect_key,
        scope_key=scope_key,
        response_id=response_id,
        payload=payload,
    )
    if existing is not None:
        return existing

    effect = SurveyResponseEffect(
        tenant_id=tenant_id,
        survey_id=survey_id,
        response_id=response_id,
        effect_type=effect_type,
        effect_key=effect_key,
        scope_key=scope_key,
        payload_json=dict(payload),
        status=STATUS_PENDING,
        attempt_count=0,
        max_attempts=max_attempts,
        available_at=now,
        contract_version=OUTBOX_CONTRACT_VERSION,
    )

    # A SAVEPOINT confines a concurrent unique-scope collision to this one
    # insert.  Never call session.rollback() here: the outer response and its
    # receipt belong to the caller and may be using commit=False (Meta Flow).
    try:
        _ensure_physical_outer_transaction_for_savepoint()
        with db.session.begin_nested():
            db.session.add(effect)
            db.session.flush()
        return effect
    except IntegrityError:
        existing = _find_existing_effect(
            tenant_id=tenant_id,
            effect_type=effect_type,
            effect_key=effect_key,
            scope_key=scope_key,
            response_id=response_id,
            payload=payload,
        )
        if existing is None:
            raise
        return existing


def stage_survey_response_effects(
    encuesta: EncEncuesta,
    respuesta: EncRespuesta,
    *,
    slug_publico: Optional[str] = None,
    respuestas_payload: Optional[Sequence[Mapping[str, Any]]] = None,
    authenticated_user: Optional[User] = None,
    grant_reward: bool = True,
    emit_realtime_update: bool = True,
    stage_analytics: bool = True,
    now: Optional[datetime] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[SurveyResponseEffect]:
    """Stage every applicable effect without owning the outer transaction.

    Only identifiers, counters and policy snapshots are persisted.  In
    particular, the outbox never receives DNI, phone, response free text,
    coordinates, fingerprints, bearer tokens, or raw submission keys.
    """

    tenant_id = _positive_int(getattr(encuesta, "tenant_id", None), field="tenant_id")
    survey_id = _positive_int(getattr(encuesta, "id", None), field="survey_id")
    response_id = _positive_int(getattr(respuesta, "id", None), field="response_id")
    if int(getattr(respuesta, "tenant_id", 0) or 0) != tenant_id:
        raise ValueError("response tenant does not match survey tenant")
    if int(getattr(respuesta, "encuesta_id", 0) or 0) != survey_id:
        raise ValueError("response survey does not match staged survey")

    max_attempts = _positive_int(max_attempts, field="max_attempts")
    staged_at = _coerce_utc(now)
    public_slug = str(slug_publico or getattr(encuesta, "slug", "") or "").strip()
    response_scope = _response_scope(survey_id, response_id)
    staged: list[SurveyResponseEffect] = []

    if stage_analytics:
        analytics_key = _effect_key(response_id, EFFECT_ANALYTICS)
        staged.append(
            _stage_one(
                tenant_id=tenant_id,
                survey_id=survey_id,
                response_id=response_id,
                effect_type=EFFECT_ANALYTICS,
                scope_key=response_scope,
                payload={
                    "contract_version": ANALYTICS_PAYLOAD_CONTRACT,
                    "survey_id": survey_id,
                    "response_id": response_id,
                    "slug": public_slug,
                    "answers_count": _answers_count(respuesta, respuestas_payload),
                    "event_id": _stable_event_id(analytics_key),
                },
                now=staged_at,
                max_attempts=max_attempts,
            )
        )

    reward_points = 0
    try:
        reward_points = int(getattr(encuesta, "puntos_recompensa", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        reward_points = 0
    user_id = getattr(authenticated_user, "id", None)
    if grant_reward and reward_points > 0 and user_id is not None:
        user_id = _positive_int(user_id, field="authenticated_user.id")
        idempotency_key = _reward_idempotency_key(survey_id, user_id)
        staged.append(
            _stage_one(
                tenant_id=tenant_id,
                survey_id=survey_id,
                response_id=response_id,
                effect_type=EFFECT_REWARD,
                scope_key=_reward_scope(survey_id, user_id),
                payload={
                    "contract_version": REWARD_PAYLOAD_CONTRACT,
                    "survey_id": survey_id,
                    "response_id": response_id,
                    "user_id": user_id,
                    "points": reward_points,
                    "idempotency_key": idempotency_key,
                },
                now=staged_at,
                max_attempts=max_attempts,
            )
        )

    if emit_realtime_update and bool(
        getattr(encuesta, "mostrar_resultados_envivo", False)
    ):
        realtime_key = _effect_key(response_id, EFFECT_REALTIME)
        staged.append(
            _stage_one(
                tenant_id=tenant_id,
                survey_id=survey_id,
                response_id=response_id,
                effect_type=EFFECT_REALTIME,
                scope_key=response_scope,
                payload={
                    "contract_version": REALTIME_PAYLOAD_CONTRACT,
                    "event_id": _stable_event_id(realtime_key),
                    "event_name": "survey.response.committed",
                    "tenant_id": tenant_id,
                    "survey_id": survey_id,
                    "response_id": response_id,
                    "slug": public_slug,
                },
                now=staged_at,
                max_attempts=max_attempts,
            )
        )

    return staged


def _load_scoped_source(
    effect: SurveyResponseEffect,
) -> tuple[EncEncuesta, EncRespuesta]:
    encuesta = db.session.get(EncEncuesta, int(effect.survey_id))
    respuesta = db.session.get(EncRespuesta, int(effect.response_id))
    if encuesta is None or respuesta is None:
        raise _PermanentEffectError("source_missing")
    if (
        int(encuesta.tenant_id) != int(effect.tenant_id)
        or int(respuesta.tenant_id) != int(effect.tenant_id)
        or int(respuesta.encuesta_id) != int(effect.survey_id)
    ):
        raise _PermanentEffectError("source_scope_mismatch")
    return encuesta, respuesta


def _analytics_outcome(effect: SurveyResponseEffect) -> _EffectOutcome:
    encuesta, respuesta = _load_scoped_source(effect)
    payload = effect.payload_json if isinstance(effect.payload_json, dict) else {}
    event_id = str(payload.get("event_id") or _stable_event_id(effect.effect_key))
    if event_id != _stable_event_id(effect.effect_key):
        raise _PermanentEffectError("analytics_event_id_invalid")

    selected_options: list[dict[str, int]] = []
    open_answers = 0
    for detalle in getattr(respuesta, "detalles", None) or []:
        if getattr(detalle, "opcion_id", None) is not None:
            selected_options.append(
                {
                    "pregunta_id": int(detalle.pregunta_id),
                    "opcion_id": int(detalle.opcion_id),
                }
            )
        if getattr(detalle, "texto_libre", None):
            open_answers += 1

    is_live_vote = bool(getattr(encuesta, "es_votacion_envivo", False)) or str(
        getattr(encuesta, "tipo", "") or ""
    ).strip().lower() in {"votacion", "votacion_envivo", "live_vote"}
    tenant = db.session.get(TenantProfile, int(effect.tenant_id))
    tenant_type = str(getattr(tenant, "tipo", None) or "municipio")[:20]
    public_slug = str(payload.get("slug") or getattr(encuesta, "slug", "") or "")
    analytics_payload = {
        "contract_version": ANALYTICS_PAYLOAD_CONTRACT,
        "encuesta_id": int(encuesta.id),
        "survey_id": int(encuesta.id),
        "slug": public_slug,
        "slug_publico": public_slug,
        "response_id": int(respuesta.id),
        "respuesta_id": int(respuesta.id),
        "survey_type": getattr(encuesta, "tipo", None),
        "is_live_vote": is_live_vote,
        "live_results_visible": bool(
            getattr(encuesta, "mostrar_resultados_envivo", False)
        ),
        "answers_count": max(0, int(payload.get("answers_count") or 0)),
        "selected_options_count": len(selected_options),
        "open_answers_count": open_answers,
        "selected_options": selected_options[:40],
        "has_geo": respuesta.lat is not None and respuesta.lng is not None,
        "has_contact_identity": bool(
            respuesta.user_id or respuesta.dni or respuesta.phone
        ),
        "has_demographics": bool(
            respuesta.genero or respuesta.rango_etario or respuesta.edad
        ),
        "utm_source": respuesta.utm_source,
        "utm_campaign": respuesta.utm_campaign,
        "barrio": respuesta.barrio,
        "ciudad": respuesta.ciudad,
        "provincia": respuesta.provincia,
        "pais": respuesta.pais,
    }

    # Lazy import avoids a circular dependency with encuestas_service.
    from services.analytics.ingestor import analytics_ingestor

    event_name = "vote_submitted" if is_live_vote else "survey_answer_submitted"
    entity_ref = f"survey:{encuesta.id}:response:{respuesta.id}"
    event = analytics_ingestor.track(
        tenant_id=int(effect.tenant_id),
        event_name=event_name,
        payload=analytics_payload,
        event_id=event_id,
        user_id=int(respuesta.user_id) if respuesta.user_id else None,
        anon_id=respuesta.huella_unica or None,
        channel=respuesta.canal or "public_survey",
        session_id=respuesta.huella_unica or None,
        lat=respuesta.lat,
        lng=respuesta.lng,
        entity_ref=entity_ref,
        tenant_type=tenant_type,
        commit=False,
        raise_on_error=True,
    )
    if event is None:
        raise RuntimeError("analytics_ingest_not_confirmed")
    event_payload = getattr(event, "metadata_payload", None)
    try:
        event_tenant_id = int(getattr(event, "tenant_id", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        event_tenant_id = 0
    if (
        str(getattr(event, "id", "") or "") != event_id
        or event_tenant_id != int(effect.tenant_id)
        or str(getattr(event, "event_name", "") or "") != event_name
        or str(getattr(event, "entity_ref", "") or "") != entity_ref
        or not isinstance(event_payload, dict)
        or event_payload != analytics_payload
    ):
        # A deterministic UUID is an idempotency key, not proof that an
        # existing row represents this source event.  Never bless a collision
        # belonging to another tenant, entity, contract, or payload.
        raise _PermanentEffectError("analytics_event_collision_mismatch")
    return _EffectOutcome(
        status=STATUS_SUCCEEDED,
        result={"event_id": event_id, "event_name": event_name},
    )


def _reward_outcome(effect: SurveyResponseEffect) -> _EffectOutcome:
    encuesta, respuesta = _load_scoped_source(effect)
    payload = effect.payload_json if isinstance(effect.payload_json, dict) else {}
    user_id = _positive_int(payload.get("user_id"), field="reward.user_id")
    points = _positive_int(payload.get("points"), field="reward.points")
    expected_idempotency_key = _reward_idempotency_key(encuesta.id, user_id)
    idempotency_key = str(payload.get("idempotency_key") or "")
    if idempotency_key != expected_idempotency_key:
        raise _PermanentEffectError("reward_idempotency_key_invalid")
    user = db.session.get(User, user_id)
    if user is None or int(respuesta.user_id or 0) != user_id:
        raise _PermanentEffectError("reward_user_invalid")

    from services.encuestas_service import _grant_survey_reward_effect

    disposition = _grant_survey_reward_effect(
        encuesta,
        respuesta,
        user,
        reward_points=points,
        idempotency_key=idempotency_key,
        commit=False,
    )
    if disposition not in {"credited", "already_credited", "skipped"}:
        raise RuntimeError("reward_disposition_invalid")
    if disposition == "skipped":
        return _EffectOutcome(
            status=STATUS_SKIPPED,
            result={"disposition": disposition, "points": points},
        )
    return _EffectOutcome(
        status=STATUS_SUCCEEDED,
        result={"disposition": disposition, "points": points},
    )


def _realtime_outcome(effect: SurveyResponseEffect) -> _EffectOutcome:
    encuesta, _respuesta = _load_scoped_source(effect)
    payload = effect.payload_json if isinstance(effect.payload_json, dict) else {}
    if not bool(getattr(encuesta, "mostrar_resultados_envivo", False)):
        return _EffectOutcome(
            status=STATUS_SKIPPED,
            result={"reason_code": "realtime_disabled_after_submission"},
        )

    event_id = str(payload.get("event_id") or _stable_event_id(effect.effect_key))
    if event_id != _stable_event_id(effect.effect_key):
        raise _PermanentEffectError("realtime_event_id_invalid")
    envelope = {
        "contract_version": REALTIME_PAYLOAD_CONTRACT,
        "event_id": event_id,
        "event_name": "survey.response.committed",
        "tenant_id": int(effect.tenant_id),
        "survey_id": int(effect.survey_id),
        "response_id": int(effect.response_id),
        "slug": str(payload.get("slug") or getattr(encuesta, "slug", "") or ""),
    }

    from services.encuestas_service import emit_survey_response_update

    emitted = emit_survey_response_update(
        encuesta,
        envelope["slug"],
        event_envelope=envelope,
    )
    if emitted is not True:
        raise RuntimeError("realtime_emit_not_confirmed")
    return _EffectOutcome(
        status=STATUS_SUCCEEDED,
        result={"delivery": "emitted", "envelope": envelope},
    )


def _execute_effect(effect: SurveyResponseEffect) -> _EffectOutcome:
    if effect.effect_type == EFFECT_ANALYTICS:
        return _analytics_outcome(effect)
    if effect.effect_type == EFFECT_REWARD:
        return _reward_outcome(effect)
    if effect.effect_type == EFFECT_REALTIME:
        return _realtime_outcome(effect)
    raise _PermanentEffectError("unsupported_effect_type")


def _due_filter(now: datetime):
    return or_(
        and_(
            SurveyResponseEffect.status.in_([STATUS_PENDING, STATUS_RETRY_WAIT]),
            SurveyResponseEffect.attempt_count < SurveyResponseEffect.max_attempts,
            SurveyResponseEffect.available_at <= now,
        ),
        and_(
            SurveyResponseEffect.status == STATUS_PROCESSING,
            SurveyResponseEffect.leased_until.isnot(None),
            SurveyResponseEffect.leased_until <= now,
        ),
    )


def _claim_effect(effect_id: int, *, now: datetime) -> Optional[str]:
    token = secrets.token_hex(24)
    statement = (
        update(SurveyResponseEffect)
        .where(SurveyResponseEffect.id == int(effect_id))
        .where(_due_filter(now))
        .values(
            status=STATUS_PROCESSING,
            # Reclaiming an expired lease resumes the abandoned attempt.  This
            # avoids violating attempt_count <= max_attempts when a worker dies
            # after claiming its final allowed attempt but before finalizing.
            attempt_count=case(
                (
                    SurveyResponseEffect.status == STATUS_PROCESSING,
                    SurveyResponseEffect.attempt_count,
                ),
                else_=SurveyResponseEffect.attempt_count + 1,
            ),
            lease_token=token,
            leased_until=now + timedelta(seconds=LEASE_SECONDS),
            processed_at=None,
            last_error=None,
            updated_at=now,
        )
    )
    result = db.session.execute(
        statement.execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return token


def _finalize_effect(
    effect_id: int,
    lease_token: str,
    outcome: _EffectOutcome,
    *,
    now: datetime,
) -> bool:
    if outcome.status not in {STATUS_SUCCEEDED, STATUS_SKIPPED}:
        raise ValueError("invalid successful terminal outcome")
    statement = (
        update(SurveyResponseEffect)
        .where(
            SurveyResponseEffect.id == int(effect_id),
            SurveyResponseEffect.status == STATUS_PROCESSING,
            SurveyResponseEffect.lease_token == lease_token,
        )
        .values(
            status=outcome.status,
            result_json=dict(outcome.result),
            processed_at=now,
            lease_token=None,
            leased_until=None,
            last_error=None,
            updated_at=now,
        )
    )
    result = db.session.execute(
        statement.execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        # Handler DB writes and the status transition share this transaction.
        # A stale worker must lose both when its lease fence no longer matches.
        db.session.rollback()
        return False
    db.session.commit()
    return True


def _sanitized_error(exc: BaseException) -> str:
    code = getattr(exc, "code", None) or type(exc).__name__
    normalized = _SAFE_ERROR_CODE.sub("_", str(code)).strip("_.:-")
    if not normalized:
        normalized = "effect_handler_error"
    return normalized[:96]


def _retry_delay(attempt_count: int) -> int:
    exponent = max(0, min(int(attempt_count) - 1, 16))
    return min(BASE_BACKOFF_SECONDS * (2**exponent), MAX_BACKOFF_SECONDS)


def _record_failure(
    effect_id: int,
    lease_token: str,
    *,
    attempt_count: int,
    max_attempts: int,
    error_code: str,
    permanent: bool,
    now: datetime,
) -> Optional[str]:
    dead = permanent or int(attempt_count) >= int(max_attempts)
    target_status = STATUS_DEAD if dead else STATUS_RETRY_WAIT
    available_at = now if dead else now + timedelta(seconds=_retry_delay(attempt_count))
    statement = (
        update(SurveyResponseEffect)
        .where(
            SurveyResponseEffect.id == int(effect_id),
            SurveyResponseEffect.status == STATUS_PROCESSING,
            SurveyResponseEffect.lease_token == lease_token,
        )
        .values(
            status=target_status,
            available_at=available_at,
            processed_at=now if dead else None,
            lease_token=None,
            leased_until=None,
            result_json={"reason_code": error_code} if dead else None,
            last_error=error_code,
            updated_at=now,
        )
    )
    result = db.session.execute(
        statement.execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        db.session.rollback()
        return None
    db.session.commit()
    return target_status


def dispatch_survey_response_effects(
    tenant_id: Optional[int] = None,
    response_id: Optional[int] = None,
    limit: int = 50,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Claim and process due effects with lease fencing and bounded retries.

    This is a worker boundary and therefore commits its own claim and terminal
    transitions.  Do not invoke it from inside an uncommitted response write.
    """

    operation_now = _coerce_utc(now)
    bounded_limit = _bounded_limit(limit)
    query = db.session.query(SurveyResponseEffect.id).filter(
        _due_filter(operation_now)
    )
    if tenant_id is not None:
        query = query.filter(
            SurveyResponseEffect.tenant_id
            == _positive_int(tenant_id, field="tenant_id")
        )
    if response_id is not None:
        query = query.filter(
            SurveyResponseEffect.response_id
            == _positive_int(response_id, field="response_id")
        )

    # Fetch extra candidates so CAS losers do not starve a small batch when
    # another worker claimed the same initial snapshot.
    candidate_ids = [
        int(row[0])
        for row in query.order_by(
            SurveyResponseEffect.available_at.asc(),
            SurveyResponseEffect.id.asc(),
        )
        .limit(min(bounded_limit * 4, MAX_DISPATCH_LIMIT * 4))
        .all()
    ]
    stats: dict[str, Any] = {
        "contract_version": OUTBOX_CONTRACT_VERSION,
        "claimed": 0,
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "retry_wait": 0,
        "dead": 0,
        "fenced": 0,
    }

    for effect_id in candidate_ids:
        if stats["claimed"] >= bounded_limit:
            break
        lease_token = _claim_effect(effect_id, now=operation_now)
        if lease_token is None:
            continue
        stats["claimed"] += 1

        effect = db.session.get(
            SurveyResponseEffect,
            effect_id,
            populate_existing=True,
        )
        if (
            effect is None
            or effect.status != STATUS_PROCESSING
            or effect.lease_token != lease_token
        ):
            db.session.rollback()
            stats["fenced"] += 1
            continue

        attempt_count = int(effect.attempt_count or 0)
        max_attempts = int(effect.max_attempts or DEFAULT_MAX_ATTEMPTS)
        try:
            outcome = _execute_effect(effect)
            if not _finalize_effect(
                effect_id,
                lease_token,
                outcome,
                now=operation_now,
            ):
                stats["fenced"] += 1
                continue
            stats["processed"] += 1
            stats[outcome.status] += 1
        except Exception as exc:
            db.session.rollback()
            error_code = _sanitized_error(exc)
            target_status = _record_failure(
                effect_id,
                lease_token,
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                error_code=error_code,
                permanent=isinstance(exc, _PermanentEffectError),
                now=operation_now,
            )
            if target_status is None:
                stats["fenced"] += 1
                continue
            stats["processed"] += 1
            stats[target_status] += 1

    return stats


def summarize_survey_response_effects(tenant_id: int) -> dict[str, Any]:
    """Return an operator-safe health summary for one tenant's outbox."""

    resolved_tenant_id = _positive_int(tenant_id, field="tenant_id")
    rows = (
        db.session.query(
            SurveyResponseEffect.effect_type,
            SurveyResponseEffect.status,
            func.count(SurveyResponseEffect.id),
        )
        .filter(SurveyResponseEffect.tenant_id == resolved_tenant_id)
        .group_by(SurveyResponseEffect.effect_type, SurveyResponseEffect.status)
        .all()
    )
    by_status = {status: 0 for status in (
        STATUS_PENDING,
        STATUS_PROCESSING,
        STATUS_RETRY_WAIT,
        STATUS_SUCCEEDED,
        STATUS_SKIPPED,
        STATUS_DEAD,
    )}
    by_effect_type: dict[str, dict[str, int]] = {}
    total = 0
    for effect_type, status, count in rows:
        parsed_count = int(count or 0)
        total += parsed_count
        by_status[str(status)] = by_status.get(str(status), 0) + parsed_count
        bucket = by_effect_type.setdefault(str(effect_type), {})
        bucket[str(status)] = parsed_count

    now = _utc_now()
    due = (
        SurveyResponseEffect.query.filter(
            SurveyResponseEffect.tenant_id == resolved_tenant_id,
            _due_filter(now),
        ).count()
    )
    oldest_due = (
        SurveyResponseEffect.query.filter(
            SurveyResponseEffect.tenant_id == resolved_tenant_id,
            _due_filter(now),
        )
        .order_by(SurveyResponseEffect.available_at.asc())
        .first()
    )
    oldest_available_at = (
        oldest_due.available_at.isoformat()
        if oldest_due is not None and oldest_due.available_at is not None
        else None
    )
    return {
        "contract_version": SUMMARY_CONTRACT_VERSION,
        "tenant_id": resolved_tenant_id,
        "total": total,
        "due": int(due),
        "in_flight": by_status.get(STATUS_PROCESSING, 0),
        "dead": by_status.get(STATUS_DEAD, 0),
        "oldest_due_available_at": oldest_available_at,
        "by_status": by_status,
        "by_effect_type": by_effect_type,
    }


__all__ = [
    "EFFECT_ANALYTICS",
    "EFFECT_REWARD",
    "EFFECT_REALTIME",
    "STATUS_PENDING",
    "STATUS_PROCESSING",
    "STATUS_RETRY_WAIT",
    "STATUS_SUCCEEDED",
    "STATUS_SKIPPED",
    "STATUS_DEAD",
    "stage_survey_response_effects",
    "dispatch_survey_response_effects",
    "summarize_survey_response_effects",
]
