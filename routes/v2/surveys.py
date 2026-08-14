from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
from typing import Any, Mapping
from urllib.parse import quote_plus, urlencode
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import SurveyDraft, SurveyDraftIdempotency, TenantProfile
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.encuestas_analytics_service import get_dashboard_bundle
from services.encuestas_analytics_service import calculate_live_results
from services.encuestas_service import (
    EncuestaError,
    cerrar_encuesta,
    create_encuesta,
    find_survey_response_replay,
    get_encuesta,
    get_public_encuesta,
    list_encuestas,
    publicar_encuesta,
    resolve_survey_submission_id,
    resolve_optional_survey_bearer_user,
    save_respuesta,
    serialize_encuesta,
    serialize_public_encuesta,
    survey_response_receipt_contract,
    update_encuesta,
)
from services.demo_surveys import (
    build_demo_live_results_payload,
    build_demo_public_survey_payload,
    build_demo_survey_response_ack,
    is_demo_survey_slug,
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_integration_feature,
)
from services.public_survey_intake import (
    attach_public_survey_rate_limit_headers,
    enforce_public_survey_intake,
    enforce_public_survey_replay_scope,
    public_survey_client_ip,
    survey_frontend_security_contract,
    survey_security_contract,
)
from services.survey_draft_materialization import (
    SurveyDraftMaterializationError,
    materialize_survey_draft,
    serialize_materialization_result,
)
from services.survey_response_effects import (
    dispatch_survey_response_effects,
    summarize_survey_response_effects,
)
from services.survey_tenant_scope import (
    SurveyTenantScopeError,
    resolve_survey_storage_tenant_profile,
)
from services.survey_access_policy import (
    SURVEY_ELIGIBILITY_MANAGE_CAPABILITY,
    SURVEY_GOVERNANCE_MANAGE_CAPABILITY,
    SURVEY_PII_READ_CAPABILITY,
    missing_survey_capabilities,
)
from services.survey_eligibility import SURVEY_ELIGIBILITY_CREDENTIAL_HEADER
from utils.auth_helpers import token_requerido
from utils.permissions import require_role
from utils.roles import is_authorized_superadmin_user, normalize_tenant_slug
from utils.turnstile import (
    turnstile_enforce_public_intake,
    verify_turnstile,
)

v2_surveys_bp = Blueprint("v2_surveys", __name__, url_prefix="/api/v2")
v2_public_surveys_bp = Blueprint("v2_public_surveys", __name__, url_prefix="/api/v2/public/surveys")

_RESPONSE_EFFECT_SUMMARY_CONTRACT = "surveys.response_effects.admin_summary.v1"
_RESPONSE_EFFECT_RECONCILE_CONTRACT = "surveys.response_effects.reconcile.v1"
_DEFAULT_RESPONSE_EFFECT_RECONCILE_LIMIT = 50
_MAX_RESPONSE_EFFECT_RECONCILE_LIMIT = 100
_OPAQUE_ELIGIBILITY_SUBJECT_RE = re.compile(r"^subj_[A-Za-z0-9_-]{43}$")
_ELIGIBILITY_AUTHORITY_NAMESPACE_V1 = "chatboc_manual_review"
_ELIGIBILITY_SUBJECT_NAMESPACE_V1 = _ELIGIBILITY_AUTHORITY_NAMESPACE_V1
_ELIGIBILITY_AUTHORITY_ADAPTER_V1 = "manual_review.v1"

_TYPE_MAP = {
    "single": "opcion_unica",
    "single_choice": "opcion_unica",
    "single-choice": "opcion_unica",
    "singlechoice": "opcion_unica",
    "radio": "opcion_unica",
    "opcion_unica": "opcion_unica",
    "multi": "opcion_multiple",
    "multiple": "opcion_multiple",
    "multiple_choice": "opcion_multiple",
    "multiple-choice": "opcion_multiple",
    "multiplechoice": "opcion_multiple",
    "multi_choice": "opcion_multiple",
    "multi-choice": "opcion_multiple",
    "multichoice": "opcion_multiple",
    "multi_select": "opcion_multiple",
    "multi-select": "opcion_multiple",
    "multiselect": "opcion_multiple",
    "checkbox": "opcion_multiple",
    "check": "opcion_multiple",
    "multiple answers": "opcion_multiple",
    "opcion multiple": "opcion_multiple",
    "opcion_multiple": "opcion_multiple",
    "rating": "rating_emoji",
    "emoji_rating": "rating_emoji",
    "rating_emoji": "rating_emoji",
    "emoji": "rating_emoji",
    "text": "abierta",
    "free_text": "abierta",
    "open_text": "abierta",
    "open-text": "abierta",
    "open": "abierta",
    "texto": "abierta",
    "abierta": "abierta",
}
_LOSSY_UNSUPPORTED_QUESTION_TYPES = frozenset({"nps", "ranking", "location"})


def _request_id() -> str:
    incoming = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
        or ""
    )
    request_id = str(incoming).strip() or uuid.uuid4().hex
    g.request_id = request_id
    return request_id


def _json_response(payload: dict[str, Any], status: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(message: str, status_code: int, reason_code: str = "request_error", action_hint: str = "check_request"):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": False,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
        },
        status_code,
    )


def _encuesta_error_response(exc: EncuestaError):
    payload = exc.to_dict() if hasattr(exc, "to_dict") else {"message": str(exc)}
    status_code = int(getattr(exc, "status_code", None) or payload.get("status_code") or 500)
    raw_error = payload.get("error")
    message = (
        payload.get("message")
        or (raw_error.get("message") if isinstance(raw_error, dict) else None)
        or (raw_error if isinstance(raw_error, str) else None)
        or str(exc)
    )
    reason_code = payload.get("reason_code") or "survey_error"
    error_payload = raw_error if isinstance(raw_error, dict) else {"code": status_code, "message": message}
    return _json_response(
        {
            **payload,
            "contract_version": payload.get("contract_version") or "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": bool(payload.get("retryable", status_code >= 500)),
            "action_hint": payload.get("action_hint") or ("retry_later" if status_code == 429 else "check_request"),
            "error": error_payload,
            "message": message,
        },
        status_code,
    )


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def _public_client_ip() -> str:
    return public_survey_client_ip()


def _public_anon_id(payload: dict[str, Any]) -> Any:
    return (
        request.headers.get("X-Anon-Id")
        or request.headers.get("Anon-Id")
        or request.cookies.get(current_app.config.get("ANON_SESSION_COOKIE_NAME", "chatboc_anon_id"))
        or request.cookies.get("anon_id")
        or request.cookies.get("Anon-Id")
        or payload.get("anon_id")
        or payload.get("anonId")
    )


def _survey_capability_error(required: list[str], missing: list[str]):
    message = "No tenes permisos para acceder a datos sensibles de la encuesta."
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": 403,
            "reason_code": "survey_pii_read_capability_required",
            "retryable": False,
            "action_hint": "request_capability_from_tenant_admin",
            "required_capabilities": list(required),
            "missing_capabilities": list(missing),
            "error": {"code": 403, "message": message},
            "message": message,
        },
        403,
    )


def _survey_governance_capability_error(missing: list[str]):
    message = "No tenes permisos para administrar releases de gobernanza."
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": 403,
            "reason_code": "survey_governance_manage_capability_required",
            "retryable": False,
            "action_hint": "request_capability_from_tenant_admin",
            "required_capabilities": [SURVEY_GOVERNANCE_MANAGE_CAPABILITY],
            "missing_capabilities": list(missing),
            "error": {"code": 403, "message": message},
            "message": message,
        },
        403,
    )


def _no_store_response(result):
    response = result[0] if isinstance(result, tuple) else result
    if hasattr(response, "headers"):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return result


def _survey_eligibility_json_response(payload: dict[str, Any], status: int = 200):
    return _no_store_response(_json_response(payload, status))


def _survey_eligibility_capability_error(missing: list[str]):
    message = "No tenes permisos para administrar elegibilidad de encuestas."
    return _survey_eligibility_json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": 403,
            "reason_code": "survey_eligibility_manage_capability_required",
            "retryable": False,
            "action_hint": "request_capability_from_tenant_admin",
            "required_capabilities": [SURVEY_ELIGIBILITY_MANAGE_CAPABILITY],
            "missing_capabilities": list(missing),
            "error": {"code": 403, "message": message},
            "message": message,
        },
        403,
    )


def _survey_eligibility_error_response(exc):
    payload = exc.to_dict()
    payload.setdefault("status_code", int(exc.status_code))
    return _survey_eligibility_json_response(payload, int(exc.status_code))


def _survey_eligibility_release_scope_error(
    *, tenant_id: int, survey_id: int, release_id: int
):
    from models import EncEncuesta
    from models_survey_governance import SurveyGovernanceRelease

    survey_exists = EncEncuesta.query.filter_by(
        id=survey_id,
        tenant_id=tenant_id,
    ).first()
    release_exists = SurveyGovernanceRelease.query.filter_by(
        id=release_id,
        survey_id=survey_id,
        tenant_id=tenant_id,
    ).first()
    if survey_exists is not None and release_exists is not None:
        return None
    return _survey_eligibility_json_response(
        {
            "ok": False,
            "contract_version": "surveys.eligibility_grant.v1",
            "reason_code": "survey_governance_release_not_found",
            "retryable": False,
            "action_hint": "check_release_scope",
            "message": "Release no encontrado.",
        },
        404,
    )


def _attach_rate_limit_headers(response, telemetry: dict[str, Any]):
    return attach_public_survey_rate_limit_headers(response, telemetry)


def _survey_security_contract(
    *,
    status: str | None = None,
    reason: str | None = None,
    retryable: bool | None = None,
    reset_required: bool | None = None,
) -> dict[str, Any]:
    return survey_security_contract(
        status=status,
        reason=reason,
        retryable=retryable,
        reset_required=reset_required,
    )


def _survey_frontend_security_contract(
    security: dict[str, Any],
    *,
    render_as: str = "public_survey_response",
    can_retry: bool | None = None,
    reset_turnstile: bool | None = None,
) -> dict[str, Any]:
    security_payload = dict(security)
    if can_retry is not None:
        security_payload["retryable"] = bool(can_retry)
    if reset_turnstile is not None:
        security_payload["reset_required"] = bool(reset_turnstile)
    return survey_frontend_security_contract(security_payload, render_as=render_as)


def _survey_plan_required_response(tenant):
    return _json_response(
        integration_plan_required_payload(
            tenant,
            "surveys_votings",
            render_as="integration_locked",
        ),
        403,
    )


def _survey_writes_allowed(tenant) -> bool:
    return plan_allows_integration_feature(tenant, "surveys_votings")


def _stable_id_suffix(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"_", "-"}).strip("_-")
    return text[:48] or uuid.uuid4().hex[:12]


_DEFAULT_SURVEY_DRAFT_MAX_BYTES = 512 * 1024
_DEFAULT_SURVEY_DRAFT_SCHEMA_VERSION = "survey-draft.v1"
_MATERIALIZATION_IDEMPOTENCY_KEY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$"
)
_SURVEY_DRAFT_CONTROL_FIELDS = frozenset(
    {"draft_id", "id", "idempotency_key", "revision", "schema_version"}
)


def _survey_draft_content(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in _SURVEY_DRAFT_CONTROL_FIELDS}


def _survey_draft_fingerprint(payload: dict[str, Any]) -> tuple[str, int]:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), len(canonical)


def _survey_draft_max_bytes() -> int:
    configured = current_app.config.get("SURVEY_DRAFT_MAX_BYTES")
    if configured is None:
        configured = os.getenv("SURVEY_DRAFT_MAX_BYTES")
    return _coerce_positive_int(configured, _DEFAULT_SURVEY_DRAFT_MAX_BYTES)


def _survey_draft_too_large_response(max_bytes: int):
    return _error_response(
        f"El borrador no puede superar {max_bytes} bytes",
        413,
        "survey_draft_too_large",
        "reduce_draft_size",
    )


def _survey_draft_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize_survey_draft(
    draft: SurveyDraft,
    *,
    tenant=None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    body = {
        "ok": True,
        "persisted": True,
        "contract_version": "surveys.draft.v2",
        "draft_id": draft.draft_id,
        "status": "draft",
        "idempotency_key": draft.idempotency_key,
        "schema_version": draft.schema_version,
        "revision": draft.revision,
        "created_at": _survey_draft_iso(draft.created_at),
        "updated_at": _survey_draft_iso(draft.updated_at),
        "draft": draft.payload or {},
    }
    if tenant is not None:
        body["tenant"] = {"id": tenant.id, "slug": tenant.slug}
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return body


def _survey_draft_conflict_response(draft_id: str, current: SurveyDraft | None):
    current_revision = current.revision if current is not None else 0
    body = {
        "contract_version": "surveys.draft.v2",
        "ok": False,
        "persisted": False,
        "status_code": 409,
        "reason_code": "draft_revision_conflict",
        "retryable": False,
        "action_hint": "reload_draft",
        "message": "El borrador fue actualizado desde otra sesion.",
        "error": {"code": 409, "message": "La revision del borrador esta desactualizada."},
        "draft_id": draft_id,
        "current_revision": current_revision,
    }
    if current is not None:
        body.update(
            {
                "created_at": _survey_draft_iso(current.created_at),
                "updated_at": _survey_draft_iso(current.updated_at),
                "draft": current.payload or {},
            }
        )
    return _json_response(body, 409)


def _survey_draft_idempotency_conflict_response(
    draft_id: str,
    receipt: SurveyDraftIdempotency,
):
    return _json_response(
        {
            "contract_version": "surveys.draft.v2",
            "ok": False,
            "persisted": False,
            "status_code": 409,
            "reason_code": "draft_idempotency_conflict",
            "retryable": False,
            "action_hint": "use_new_idempotency_key",
            "message": "La clave de idempotencia ya fue aplicada a otro contenido de borrador.",
            "error": {
                "code": 409,
                "message": "idempotency_key ya fue utilizada para otra operacion.",
            },
            "draft_id": draft_id,
            "current_draft_id": receipt.draft_id,
            "current_revision": receipt.applied_revision,
        },
        409,
    )


def _parse_survey_draft_revision(value: Any) -> tuple[int | None, tuple | None]:
    if value in (None, ""):
        return None, None
    if isinstance(value, bool):
        return None, _error_response(
            "revision debe ser un entero no negativo",
            400,
            "invalid_draft_revision",
            "send_valid_revision",
        )
    if isinstance(value, float) and not value.is_integer():
        return None, _error_response(
            "revision debe ser un entero no negativo",
            400,
            "invalid_draft_revision",
            "send_valid_revision",
        )
    try:
        revision = int(value)
    except (TypeError, ValueError):
        return None, _error_response(
            "revision debe ser un entero no negativo",
            400,
            "invalid_draft_revision",
            "send_valid_revision",
        )
    if revision < 0:
        return None, _error_response(
            "revision debe ser un entero no negativo",
            400,
            "invalid_draft_revision",
            "send_valid_revision",
        )
    return revision, None


def _parse_materialization_revision(value: Any) -> tuple[int | None, tuple | None]:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None, _error_response(
            "expected_revision debe ser un entero positivo",
            400,
            "invalid_materialization_revision",
            "send_expected_revision",
        )
    return value, None


def _materialization_idempotency_key(
    payload: Mapping[str, Any],
) -> tuple[str | None, tuple | None]:
    header_value = request.headers.get("Idempotency-Key")
    body_value = payload.get("idempotency_key")
    normalized_header = (
        str(header_value).strip() if header_value not in (None, "") else None
    )
    normalized_body = (
        str(body_value).strip() if body_value not in (None, "") else None
    )
    if normalized_header and normalized_body and normalized_header != normalized_body:
        return None, _error_response(
            "Idempotency-Key y idempotency_key deben coincidir",
            400,
            "materialization_idempotency_key_mismatch",
            "send_matching_idempotency_key",
        )
    key = normalized_header or normalized_body
    if key is None:
        return None, _error_response(
            "Idempotency-Key es obligatorio para materializar un borrador",
            400,
            "missing_materialization_idempotency_key",
            "send_idempotency_key",
        )
    if not _MATERIALIZATION_IDEMPOTENCY_KEY.fullmatch(key):
        return None, _error_response(
            "Idempotency-Key debe tener entre 8 y 128 caracteres seguros",
            400,
            "invalid_materialization_idempotency_key",
            "send_valid_idempotency_key",
        )
    return key, None


def _survey_draft_receipt_matches(
    receipt: SurveyDraftIdempotency,
    *,
    draft_id: str,
    schema_version: str,
    payload_hash: str,
) -> bool:
    return (
        receipt.draft_id == draft_id
        and receipt.schema_version == schema_version
        and receipt.payload_hash == payload_hash
    )


def _persist_survey_draft_save(
    draft: SurveyDraft,
    *,
    tenant,
    idempotency_key: str | None,
    schema_version: str,
    payload_hash: str,
):
    tenant_id = tenant.id
    draft_id = draft.draft_id
    draft_payload = draft.payload
    try:
        db.session.add(draft)
        db.session.flush()
        if idempotency_key:
            receipt = SurveyDraftIdempotency(
                tenant_id=tenant.id,
                survey_draft_id=draft.id,
                idempotency_key=idempotency_key,
                draft_id=draft.draft_id,
                schema_version=schema_version,
                payload_hash=payload_hash,
                applied_revision=draft.revision,
            )
            db.session.add(receipt)
            db.session.flush()

        response_body = _serialize_survey_draft(
            draft,
            tenant=tenant,
            idempotency_key=idempotency_key,
        )
        db.session.commit()
        return _json_response(response_body)
    except IntegrityError:
        # Recover deterministically if another request wins either uniqueness race.
        db.session.rollback()
        if idempotency_key:
            raced_receipt = SurveyDraftIdempotency.query.filter_by(
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
            ).first()
            if raced_receipt is not None:
                if _survey_draft_receipt_matches(
                    raced_receipt,
                    draft_id=draft_id,
                    schema_version=schema_version,
                    payload_hash=payload_hash,
                ):
                    current = db.session.get(SurveyDraft, raced_receipt.survey_draft_id)
                    if current is not None and current.tenant_id == tenant_id:
                        return _json_response(
                            _serialize_survey_draft(
                                current,
                                tenant=tenant,
                                idempotency_key=idempotency_key,
                            )
                        )
                return _survey_draft_idempotency_conflict_response(
                    draft_id,
                    raced_receipt,
                )

        current = SurveyDraft.query.filter_by(
            tenant_id=tenant_id,
            draft_id=draft_id,
        ).first()
        if current is not None and (
            current.schema_version == schema_version
            and (current.payload_hash == payload_hash or current.payload == draft_payload)
        ):
            return _json_response(_serialize_survey_draft(current, tenant=tenant))
        return _survey_draft_conflict_response(draft_id, current)


def _resolve_tenant_or_error(*, required: bool = True):
    has_explicit_selector = any(
        header_name in request.headers
        for header_name in ("X-Tenant-Slug", "X-Tenant")
    ) or any(
        query_name in request.args
        for query_name in ("tenant_slug", "tenant")
    )
    explicit_candidates = [
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
    ]
    normalized_candidates = [
        normalize_tenant_slug(value)
        for value in explicit_candidates
        if normalize_tenant_slug(value)
    ]
    if len(set(normalized_candidates)) > 1:
        return None, _error_response(
            "Los selectores de tenant deben coincidir",
            400,
            "tenant_selector_mismatch",
            "send_matching_tenant_slug",
        )
    explicit_slug = normalized_candidates[0] if normalized_candidates else ""
    if has_explicit_selector and not explicit_slug:
        return None, _error_response(
            "Tenant no encontrado",
            404,
            "tenant_resolution_failed",
            "check_tenant_slug",
        )
    if required and not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en surveys v2", 400, "missing_tenant", "send_tenant_slug")
    try:
        tenant = resolve_tenant_v2(
            # Public survey links may omit tenant entirely. Once a caller does
            # provide one, however, an unknown slug must not dissolve into an
            # unscoped lookup or fall back to another identity source.
            required=required or bool(explicit_slug),
            explicit_slug=explicit_slug or None,
        )
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")
    if explicit_slug and (
        tenant is None
        or normalize_tenant_slug(getattr(tenant, "slug", None))
        != normalize_tenant_slug(explicit_slug)
    ):
        return None, _error_response(
            "Tenant no encontrado",
            404,
            "tenant_resolution_failed",
            "check_tenant_slug",
        )
    return tenant, None


def _clean_base_url(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip().rstrip("/")
    return None


def _public_frontend_base_url() -> str:
    for key in (
        "PUBLIC_ENCUESTAS_CANONICAL_BASE_URL",
        "PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL",
        "FRONTEND_URL",
        "PUBLIC_FRONTEND_URL",
        "APP_BASE_URL",
        "PUBLIC_BASE_URL",
    ):
        configured = _clean_base_url(current_app.config.get(key))
        if configured:
            return configured
    return request.host_url.rstrip("/")


def _public_api_base_url() -> str:
    for key in ("PUBLIC_ENCUESTAS_API_BASE_URL", "BACKEND_URL", "API_BASE_URL", "PUBLIC_API_BASE_URL"):
        configured = _clean_base_url(current_app.config.get(key))
        if configured:
            return configured
    return request.host_url.rstrip("/")


def _absolute_url(path_or_url: str | None, base_url: str) -> str | None:
    if not path_or_url:
        return None
    value = str(path_or_url)
    if value.startswith(("http://", "https://")):
        return value
    if value.startswith("/"):
        return f"{base_url}{value}"
    return f"{base_url}/{value}"


def _append_query(path: str, params: dict[str, Any] | None = None) -> str:
    clean = {
        key: value
        for key, value in (params or {}).items()
        if value not in (None, "")
    }
    if not clean:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode(clean)}"


def _tenant_slug_value(tenant=None) -> str | None:
    slug = getattr(tenant, "slug", None)
    if isinstance(slug, str) and slug.strip():
        return slug.strip()
    return None


def _tenant_slug_for_resolved_survey(encuesta) -> str | None:
    tenant_id = getattr(encuesta, "tenant_id", None)
    try:
        normalized_tenant_id = int(tenant_id)
    except (TypeError, ValueError):
        return None

    try:
        tenant = resolve_survey_storage_tenant_profile(normalized_tenant_id)
    except SurveyTenantScopeError:
        current_app.logger.error(
            "Rejected survey contract with non-canonical or ambiguous storage "
            "scope tenant_id=%s",
            normalized_tenant_id,
        )
        return None
    return _tenant_slug_value(tenant)


def _datetime_utc(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _survey_public_state(encuesta) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    estado = str(getattr(encuesta, "estado", "") or "").strip().lower()
    opens_at = _datetime_utc(getattr(encuesta, "inicio_at", None))
    closes_at = _datetime_utc(getattr(encuesta, "fin_at", None))
    is_live_vote = bool(getattr(encuesta, "es_votacion_envivo", False))

    if estado != "publicada":
        status = estado or "draft"
        accepts_responses = False
    elif opens_at and now < opens_at:
        status = "scheduled"
        accepts_responses = False
    elif closes_at and now > closes_at:
        status = "closed"
        accepts_responses = False
    else:
        status = "live" if is_live_vote else "open"
        accepts_responses = True

    return {
        "contract_version": "surveys.public_state.v2",
        "status": status,
        "is_open": accepts_responses,
        "accepts_responses": accepts_responses,
        "is_live_vote": is_live_vote,
        "results_visible": bool(getattr(encuesta, "mostrar_resultados_envivo", False)),
        "comments_enabled": bool(getattr(encuesta, "permitir_comentarios", False)),
        "opens_at": opens_at.isoformat() if opens_at else None,
        "closes_at": closes_at.isoformat() if closes_at else None,
        "server_time": now.isoformat(),
    }


def _survey_response_count(encuesta) -> int:
    respuestas = getattr(encuesta, "respuestas", None)
    if respuestas is None:
        return 0
    count_attr = getattr(respuestas, "count", None)
    if callable(count_attr):
        try:
            return int(count_attr())
        except TypeError:
            return 0
    try:
        return len(respuestas)
    except TypeError:
        return 0


def _build_survey_links(token: str, *, tenant_slug: str | None = None) -> dict[str, Any]:
    public_token = str(token or "").strip()
    tenant_query = {"tenant_slug": tenant_slug}
    public_page_path = f"/e/{public_token}" if public_token else None
    public_page_url = _absolute_url(public_page_path, _public_frontend_base_url())
    public_api_endpoint = _append_query(f"/api/v2/public/surveys/{public_token}", tenant_query)
    respond_endpoint = _append_query(f"/api/v2/public/surveys/{public_token}/respond", tenant_query)
    live_results_endpoint = _append_query(f"/api/v2/public/surveys/{public_token}/live-results", tenant_query)
    qr_endpoint = f"/api/public/encuestas/v1/{public_token}/qr?size=320"
    qr_url = _absolute_url(qr_endpoint, _public_api_base_url())
    legacy_public_api_endpoint = f"/api/public/encuestas/v1/{public_token}"
    legacy_live_results_endpoint = f"/api/public/encuestas/v1/{public_token}/live-results"

    return {
        "contract_version": "surveys.links.v2",
        "public_token": public_token,
        "public_page_path": public_page_path,
        "public_page_url": public_page_url,
        "share_url": public_page_url,
        "public_api_endpoint": public_api_endpoint,
        "respond_endpoint": respond_endpoint,
        "live_results_endpoint": live_results_endpoint,
        "qr_endpoint": qr_endpoint,
        "qr_url": qr_url,
        "qr_image_url": qr_url,
        "legacy_public_api_endpoint": legacy_public_api_endpoint,
        "legacy_live_results_endpoint": legacy_live_results_endpoint,
    }


def _build_share_contract(
    token: str,
    *,
    title: str | None = None,
    tenant_slug: str | None = None,
) -> dict[str, Any]:
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    share_url = links["public_page_url"]
    share_text = f"Participa en {title or 'esta encuesta'}: {share_url}" if share_url else None
    whatsapp_url = f"https://wa.me/?text={quote_plus(share_text)}" if share_text else None
    return {
        "contract_version": "surveys.share.v2",
        "url": share_url,
        "text": share_text,
        "whatsapp_text": share_text,
        "whatsapp_url": whatsapp_url,
        "copy": {
            "url": share_url,
            "text": share_text,
        },
        "qr": {
            "target_url": share_url,
            "image_url": links["qr_image_url"],
            "download_url": links["qr_image_url"],
            "endpoint": links["qr_endpoint"],
            "size": 320,
        },
        "channels": ["copy_link", "qr", "whatsapp"],
    }


def _build_realtime_contract(
    token: str,
    *,
    tenant_slug: str | None = None,
    enabled: bool,
    polling_interval_ms: int = 5000,
    result_version: Any = None,
    snapshot_version: Any = None,
) -> dict[str, Any]:
    normalized_token = str(token or "").strip()
    normalized_tenant = (tenant_slug or "").strip()
    socket_enabled = bool(enabled and normalized_token and normalized_tenant)
    primary_room = f"encuesta:{normalized_tenant}:{normalized_token}" if socket_enabled else None
    rooms = [primary_room] if primary_room else []
    live_results_endpoint = _build_survey_links(token, tenant_slug=tenant_slug)["live_results_endpoint"]
    return {
        "contract_version": "surveys.realtime.v2",
        "enabled": bool(enabled),
        "transports": (["socket.io", "polling"] if socket_enabled else ["polling"]) if enabled else [],
        "room": primary_room,
        "primary_room": primary_room,
        # Keep the compatibility key, but never advertise the retired unscoped room.
        "legacy_room": primary_room,
        "rooms": rooms,
        "socket": {
            "enabled": socket_enabled,
            "path": "/api/socket.io",
            "join_event": "join",
            "join_payload": {"room": primary_room} if primary_room else None,
            "join_payloads": [{"room": room} for room in rooms],
            "events": [
                {"name": "survey_update_v2", "contract_version": "surveys.live_results.v2"},
                {"name": "survey.vote.created", "contract_version": "surveys.live_results.v2"},
                {"name": "survey_update", "contract_version": "legacy"},
            ],
        },
        "polling": {
            "enabled": bool(enabled),
            "href": live_results_endpoint if enabled else None,
            "interval_ms": polling_interval_ms,
            "fallback_after_ms": 15000,
        },
        "versioning": {
            "result_version": result_version,
            "snapshot_version": snapshot_version,
            "result_version_field": "result_version",
            "snapshot_version_field": "snapshot_version",
        },
    }


def _build_survey_admin_paths(
    encuesta,
    token: str,
    *,
    tenant_slug: str | None = None,
) -> dict[str, Any]:
    survey_id = getattr(encuesta, "id", None)
    base_params = {
        "focus": "live_results",
        "survey_slug": token,
        "tenant_slug": tenant_slug,
    }
    heatmap_params = {
        **base_params,
        "focus": "heatmap",
        "include_heatmap": "1",
    }
    moderation_params = {
        **base_params,
        "focus": "moderation",
    }

    if survey_id is not None:
        detail_path = f"/admin/encuestas/{survey_id}"
        analytics_path = _append_query(f"/admin/encuestas/{survey_id}/analytics", base_params)
        heatmap_path = _append_query(f"/admin/encuestas/{survey_id}/analytics", heatmap_params)
        moderation_path = _append_query(f"/admin/encuestas/{survey_id}/analytics", moderation_params)
        analytics_endpoint = _append_query(
            f"/api/v2/surveys/{survey_id}/analytics",
            {"tenant_slug": tenant_slug},
        )
    else:
        detail_path = _append_query("/admin/encuestas", {"survey_slug": token, "tenant_slug": tenant_slug})
        analytics_path = _append_query("/admin/encuestas", base_params)
        heatmap_path = _append_query("/admin/encuestas", heatmap_params)
        moderation_path = _append_query("/admin/encuestas", moderation_params)
        analytics_endpoint = None

    base_url = _public_frontend_base_url()
    return {
        "survey_id": survey_id,
        "detail_path": detail_path,
        "detail_href": _absolute_url(detail_path, base_url),
        "analytics_path": analytics_path,
        "analytics_href": _absolute_url(analytics_path, base_url),
        "heatmap_path": heatmap_path,
        "heatmap_href": _absolute_url(heatmap_path, base_url),
        "moderation_path": moderation_path,
        "moderation_href": _absolute_url(moderation_path, base_url),
        "analytics_endpoint": analytics_endpoint,
    }


def _build_survey_operations_contract(
    encuesta,
    token: str,
    *,
    tenant_slug: str | None = None,
    live_results_enabled: bool,
    public_state: dict[str, Any] | None = None,
    responses_count: int | None = None,
) -> dict[str, Any]:
    state = public_state or {}
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    paths = _build_survey_admin_paths(encuesta, token, tenant_slug=tenant_slug)
    realtime = _build_realtime_contract(
        token,
        tenant_slug=tenant_slug,
        enabled=live_results_enabled,
    )
    comments_enabled = bool(state.get("comments_enabled"))

    actions = [
        {
            "id": "open_live_results_admin",
            "label": "Monitorear en vivo",
            "href": paths["analytics_href"],
            "frontend_path": paths["analytics_path"],
            "requires_auth": True,
            "requires_role": ["tenant_admin", "employee", "superadmin"],
            "ui_hint": "live_command_center",
            "enabled": bool(live_results_enabled),
        },
        {
            "id": "open_heatmap_admin",
            "label": "Abrir mapa de calor",
            "href": paths["heatmap_href"],
            "frontend_path": paths["heatmap_path"],
            "requires_auth": True,
            "requires_role": ["tenant_admin", "employee", "superadmin"],
            "ui_hint": "heatmap",
            "enabled": True,
        },
        {
            "id": "moderate_comments",
            "label": "Moderar comentarios",
            "href": paths["moderation_href"],
            "frontend_path": paths["moderation_path"],
            "requires_auth": True,
            "requires_role": ["tenant_admin", "employee", "superadmin"],
            "ui_hint": "moderation_queue",
            "enabled": comments_enabled,
        },
        {
            "id": "share_whatsapp_qr",
            "label": "Compartir QR por WhatsApp",
            "href": links["qr_image_url"],
            "share_url": links["share_url"],
            "requires_auth": True,
            "requires_role": ["tenant_admin", "employee", "superadmin"],
            "ui_hint": "qr_share",
            "enabled": True,
        },
    ]

    return {
        "contract_version": "surveys.operations.v2",
        "survey_id": paths["survey_id"],
        "public_token": str(token or "").strip(),
        "tenant_slug": tenant_slug,
        "status": state.get("status"),
        "is_live_vote": bool(state.get("is_live_vote")),
        "live_results_enabled": bool(live_results_enabled),
        "comments_enabled": comments_enabled,
        "responses_count": int(responses_count or 0),
        "admin_surface": {
            "id": "survey_live_ops",
            "label": "Centro operativo de encuesta",
            "route": paths["analytics_path"],
            "frontend_path": paths["analytics_path"],
            "href": paths["analytics_href"],
            "required_roles": ["tenant_admin", "employee", "superadmin"],
            "actions": actions,
        },
        "analytics_surface": {
            "id": "survey_analytics",
            "route": paths["analytics_path"],
            "frontend_path": paths["analytics_path"],
            "href": paths["analytics_href"],
            "endpoint": paths["analytics_endpoint"],
            "heatmap_route": paths["heatmap_path"],
            "heatmap_href": paths["heatmap_href"],
            "moderation_route": paths["moderation_path"],
            "moderation_href": paths["moderation_href"],
        },
        "public_surface": {
            "public_page_url": links["public_page_url"],
            "live_results_endpoint": links["live_results_endpoint"],
            "qr_image_url": links["qr_image_url"],
        },
        "realtime": realtime,
    }


def _build_operational_next_steps(
    token: str,
    *,
    tenant_slug: str | None = None,
    live_results_enabled: bool,
    public_state: dict[str, Any] | None = None,
    responses_count: int | None = None,
    operations: dict[str, Any] | None = None,
) -> dict[str, Any]:
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    realtime = _build_realtime_contract(
        token,
        tenant_slug=tenant_slug,
        enabled=live_results_enabled,
    )
    status = (public_state or {}).get("status")
    items = [
        {
            "id": "share_public_link",
            "label": "Compartir enlace publico",
            "href": links["share_url"],
            "priority": 1,
        },
        {
            "id": "download_qr",
            "label": "Descargar QR",
            "href": links["qr_image_url"],
            "priority": 2,
        },
    ]
    if live_results_enabled:
        items.append(
            {
                "id": "open_live_results",
                "label": "Abrir resultados en vivo",
                "href": links["live_results_endpoint"],
                "priority": 3,
            }
        )
        if realtime["room"]:
            items.append(
                {
                    "id": "subscribe_realtime_room",
                    "label": "Suscribirse a realtime",
                    "room": realtime["room"],
                    "legacy_room": realtime["legacy_room"],
                    "event": "survey_update_v2",
                    "priority": 4,
                }
            )
    else:
        items.append(
            {
                "id": "enable_live_results",
                "label": "Activar publicacion de resultados",
                "priority": 3,
            }
        )
    if responses_count == 0:
        items.append(
            {
                "id": "promote_survey",
                "label": "Reforzar difusion",
                "href": links["share_url"],
                "priority": 5,
            }
        )
    if operations:
        admin_surface = operations.get("admin_surface") or {}
        analytics_surface = operations.get("analytics_surface") or {}
        items.extend(
            [
                {
                    "id": "open_admin_analytics",
                    "label": "Abrir tablero operativo",
                    "href": admin_surface.get("href"),
                    "frontend_path": admin_surface.get("frontend_path"),
                    "requires_auth": True,
                    "priority": 6,
                },
                {
                    "id": "open_heatmap_admin",
                    "label": "Ver mapa de calor",
                    "href": analytics_surface.get("heatmap_href"),
                    "frontend_path": analytics_surface.get("heatmap_route"),
                    "requires_auth": True,
                    "priority": 7,
                },
            ]
        )

    return {
        "contract_version": "surveys.operational_next_steps.v2",
        "status": status,
        "items": items,
    }


def _merge_ui_actions(existing: list[dict[str, Any]] | None, additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for action in list(existing or []) + additions:
        if not isinstance(action, dict):
            continue
        action_id = str(action.get("id") or action.get("label") or len(merged))
        if action_id in seen:
            continue
        seen.add(action_id)
        merged.append(action)
    return merged


def _attach_public_contract(
    payload: dict[str, Any],
    encuesta,
    token: str,
    *,
    tenant_slug: str | None = None,
    responses_count: int | None = None,
) -> dict[str, Any]:
    title = payload.get("titulo") or payload.get("title") or getattr(encuesta, "titulo", None)
    public_state = _survey_public_state(encuesta)
    live_results_enabled = bool(getattr(encuesta, "mostrar_resultados_envivo", False))
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    realtime = _build_realtime_contract(token, tenant_slug=tenant_slug, enabled=live_results_enabled)
    operations = _build_survey_operations_contract(
        encuesta,
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=live_results_enabled,
        public_state=public_state,
        responses_count=responses_count,
    )
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=live_results_enabled,
        public_state=public_state,
        responses_count=responses_count,
        operations=operations,
    )

    payload.setdefault("contract_version", "surveys.public.v2")
    payload["public_state"] = public_state
    payload["estado_publico"] = public_state
    payload["links"] = {**(payload.get("links") or {}), **links}
    payload["share"] = _build_share_contract(token, title=title, tenant_slug=tenant_slug)
    payload["realtime"] = realtime
    payload["operations"] = operations
    payload["admin_operations"] = operations
    payload["operational_next_steps"] = next_steps
    payload["next_steps"] = next_steps["items"]
    security = _survey_security_contract(
        status="required" if turnstile_enforce_public_intake() else "not_required",
        reason="anonymous_public_survey",
        retryable=False,
        reset_required=False,
    )
    payload["security"] = security
    payload["frontend_contract"] = {
        **(payload.get("frontend_contract") or {}),
        **_survey_frontend_security_contract(security),
    }
    payload.setdefault("public_page_url", links["public_page_url"])
    payload.setdefault("public_api_endpoint", links["public_api_endpoint"])
    payload.setdefault("respond_endpoint", links["respond_endpoint"])
    payload.setdefault("live_results_endpoint", links["live_results_endpoint"])
    payload.setdefault("results_endpoint", links["live_results_endpoint"])
    payload.setdefault("qr_url", links["qr_url"])
    payload.setdefault("qr_image_url", links["qr_image_url"])
    payload["ui_actions"] = _merge_ui_actions(
        payload.get("ui_actions"),
        [
            {"id": "share_public_link", "label": "Compartir", "href": links["share_url"]},
            {"id": "download_qr", "label": "QR", "href": links["qr_image_url"]},
        ],
    )
    return payload


def _rewrite_demo_realtime_for_v2(realtime: dict[str, Any] | None, links: dict[str, Any]) -> dict[str, Any]:
    updated = dict(realtime or {})
    updated["demo_mode"] = True
    polling = dict(updated.get("polling") or {})
    polling["enabled"] = True
    polling["href"] = links["live_results_endpoint"]
    updated["polling"] = polling
    return updated


def _rewrite_demo_next_steps_for_v2(next_steps: dict[str, Any] | None, links: dict[str, Any]) -> dict[str, Any]:
    updated = dict(next_steps or {})
    items = []
    for item in list(updated.get("items") or []):
        if not isinstance(item, dict):
            continue
        cloned = dict(item)
        if cloned.get("id") == "share_public_link":
            cloned["href"] = links["share_url"]
        elif cloned.get("id") == "download_qr":
            cloned["href"] = links["qr_image_url"]
        elif cloned.get("id") == "open_live_results":
            cloned["href"] = links["live_results_endpoint"]
        items.append(cloned)
    updated["contract_version"] = "surveys.operational_next_steps.v2"
    updated["items"] = items
    return updated


def _attach_demo_public_contract(payload: dict[str, Any], token: str) -> dict[str, Any]:
    title = payload.get("titulo") or payload.get("title")
    links = _build_survey_links(token)
    share = _build_share_contract(token, title=title)
    next_steps = _rewrite_demo_next_steps_for_v2(payload.get("operational_next_steps"), links)
    security = _survey_security_contract(
        status="required" if turnstile_enforce_public_intake() else "not_required",
        reason="anonymous_demo_public_survey",
        retryable=False,
        reset_required=False,
    )

    payload["legacy_contract_version"] = payload.get("contract_version")
    payload["contract_version"] = "surveys.public.v2"
    payload["demo_mode"] = True
    payload["links"] = {**(payload.get("links") or {}), **links}
    payload["share"] = share
    payload["realtime"] = _rewrite_demo_realtime_for_v2(payload.get("realtime"), links)
    payload["operational_next_steps"] = next_steps
    payload["next_steps"] = next_steps["items"]
    payload["security"] = security
    payload["frontend_contract"] = {
        **(payload.get("frontend_contract") or {}),
        **_survey_frontend_security_contract(security),
    }
    payload["public_page_url"] = links["public_page_url"]
    payload["public_api_endpoint"] = links["public_api_endpoint"]
    payload["respond_endpoint"] = links["respond_endpoint"]
    payload["live_results_endpoint"] = links["live_results_endpoint"]
    payload["results_endpoint"] = links["live_results_endpoint"]
    payload["qr_url"] = links["qr_url"]
    payload["qr_image_url"] = links["qr_image_url"]
    payload["ui_actions"] = _merge_ui_actions(
        payload.get("ui_actions"),
        [
            {"id": "share_public_link", "label": "Compartir", "href": links["share_url"]},
            {"id": "download_qr", "label": "QR", "href": links["qr_image_url"]},
        ],
    )
    return payload


def _attach_demo_response_contract(
    payload: dict[str, Any],
    token: str,
    *,
    security: dict[str, Any],
) -> dict[str, Any]:
    links = _build_survey_links(token)
    payload["legacy_contract_version"] = payload.get("contract_version")
    payload["contract_version"] = "surveys.public_response.v2"
    payload["demo_mode"] = True
    payload["links"] = {**(payload.get("links") or {}), **links}
    payload["share"] = _build_share_contract(token, title=payload.get("title") or payload.get("titulo"))
    payload["realtime"] = _rewrite_demo_realtime_for_v2(payload.get("realtime"), links)
    payload["respond_endpoint"] = links["respond_endpoint"]
    payload["results_endpoint"] = links["live_results_endpoint"]
    payload["live_results_endpoint"] = links["live_results_endpoint"]
    payload["public_page_url"] = links["public_page_url"]
    payload["public_url"] = links["public_page_url"]
    payload["next_url"] = f"{links['public_page_url']}?resultados=1"
    payload["results_url"] = f"{links['public_page_url']}?resultados=1"
    payload["qr_url"] = links["qr_url"]
    payload["qr_image_url"] = links["qr_image_url"]
    payload["security"] = security
    payload["frontend_contract"] = _survey_frontend_security_contract(
        security,
        render_as="public_survey_response_ack",
        can_retry=False,
        reset_turnstile=False,
    )
    return payload


def _attach_demo_live_results_contract(results: dict[str, Any], token: str) -> dict[str, Any]:
    links = _build_survey_links(token)
    results["legacy_contract_version"] = results.get("contract_version")
    results["contract_version"] = "surveys.live_results.v2"
    results["demo_mode"] = True
    results["links"] = {**(results.get("links") or {}), **links}
    results["share"] = _build_share_contract(token, title=results.get("titulo") or results.get("title"))
    results["realtime"] = _rewrite_demo_realtime_for_v2(results.get("realtime"), links)
    results["live_results_endpoint"] = links["live_results_endpoint"]
    results["results_endpoint"] = links["live_results_endpoint"]
    results["public_page_url"] = links["public_page_url"]
    results["qr_url"] = links["qr_url"]
    results["qr_image_url"] = links["qr_image_url"]
    render_contract = results.setdefault("render_contract", {})
    render_contract.setdefault("preferred_visualization", "live_vote_dashboard")
    render_contract.setdefault("supports", ["cards", "bars", "timeline", "heatmap", "map_pulses"])
    render_contract.setdefault("polling_interval_ms", 8000)
    return results


def _attach_live_results_contract(
    results: dict[str, Any],
    encuesta,
    token: str,
    *,
    tenant_slug: str | None = None,
) -> dict[str, Any]:
    public_state = _survey_public_state(encuesta)
    polling_interval_ms = int(
        (results.get("live_telemetry") or {}).get("polling_interval_ms")
        or (results.get("render_contract") or {}).get("polling_interval_ms")
        or 5000
    )
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    operations = _build_survey_operations_contract(
        encuesta,
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=True,
        public_state=public_state,
        responses_count=int(results.get("total_respuestas") or 0),
    )
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=True,
        public_state=public_state,
        responses_count=int(results.get("total_respuestas") or 0),
        operations=operations,
    )

    results["public_state"] = public_state
    results["estado_publico"] = public_state
    results["links"] = {**(results.get("links") or {}), **links}
    results["share"] = _build_share_contract(
        token,
        title=getattr(encuesta, "titulo", None),
        tenant_slug=tenant_slug,
    )
    results["realtime"] = _build_realtime_contract(
        token,
        tenant_slug=tenant_slug,
        enabled=True,
        polling_interval_ms=polling_interval_ms,
        result_version=results.get("result_version"),
        snapshot_version=results.get("snapshot_version"),
    )
    results["operations"] = operations
    results["admin_operations"] = operations
    results["operational_next_steps"] = next_steps
    results["next_steps"] = next_steps["items"]

    render_contract = results.setdefault("render_contract", {})
    supports = list(render_contract.get("supports") or [])
    for capability in ("realtime_socket", "polling_fallback", "qr_share", "admin_next_steps", "admin_operations"):
        if capability not in supports:
            supports.append(capability)
    render_contract["supports"] = supports
    render_contract.setdefault("polling_interval_ms", polling_interval_ms)

    results["ui_actions"] = _merge_ui_actions(
        results.get("ui_actions"),
        [
            {"id": "share_public_link", "label": "Compartir", "href": links["share_url"]},
            {"id": "download_qr", "label": "QR", "href": links["qr_image_url"]},
            {"id": "open_public_page", "label": "Abrir encuesta", "href": links["public_page_url"]},
        ],
    )
    return results


def _user_tenant_candidates(current_user) -> set[int]:
    """Return only canonical TenantProfile foreign keys for the principal."""

    value = getattr(current_user, "tenant_id", None)
    if value in (None, ""):
        return set()
    try:
        return {int(value)}
    except (TypeError, ValueError):
        return set()


def _enforce_tenant_access(current_user, tenant) -> tuple[bool, tuple | None]:
    if is_authorized_superadmin_user(current_user):
        # Authorized superadmins may operate on the explicitly resolved tenant.
        return True, None

    candidates = _user_tenant_candidates(current_user)
    if tenant.id in candidates:
        return True, None

    # Transitional principals without user.tenant_id may use a TenantProfile
    # that points back to that same user as its owner. When the principal also
    # carries a legacy slug, it must agree with the explicitly resolved tenant.
    if not candidates:
        user_slug = (getattr(current_user, "tenant_slug", None) or "").strip().lower()
        tenant_slug = (tenant.slug or "").strip().lower()
        try:
            user_id = int(getattr(current_user, "id", 0) or 0)
        except (TypeError, ValueError):
            user_id = 0
        owner_ids = set()
        for value in (getattr(tenant, "municipio_id", None), getattr(tenant, "pyme_id", None)):
            try:
                owner_ids.add(int(value))
            except (TypeError, ValueError):
                continue
        if user_id and user_id in owner_ids and (not user_slug or user_slug == tenant_slug):
            return True, None

    return False, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")


def _response_effect_reconcile_limit() -> tuple[int | None, tuple | None]:
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return None, _error_response(
            "El cuerpo de reconciliacion debe ser un objeto JSON",
            400,
            "invalid_response_effect_reconcile_payload",
            "send_json_object",
        )

    raw_limit = payload.get("limit", request.args.get("limit"))
    if raw_limit in (None, ""):
        return _DEFAULT_RESPONSE_EFFECT_RECONCILE_LIMIT, None
    if isinstance(raw_limit, bool) or (
        isinstance(raw_limit, float) and not raw_limit.is_integer()
    ):
        parsed_limit = None
    else:
        try:
            parsed_limit = int(raw_limit)
        except (TypeError, ValueError, OverflowError):
            parsed_limit = None

    if (
        parsed_limit is None
        or parsed_limit < 1
        or parsed_limit > _MAX_RESPONSE_EFFECT_RECONCILE_LIMIT
    ):
        return None, _error_response(
            (
                "limit debe ser un entero entre 1 y "
                f"{_MAX_RESPONSE_EFFECT_RECONCILE_LIMIT}"
            ),
            400,
            "invalid_response_effect_reconcile_limit",
            "send_bounded_limit",
        )
    return parsed_limit, None


def _response_effect_summary_payload(tenant) -> dict[str, Any]:
    return {
        "contract_version": _RESPONSE_EFFECT_SUMMARY_CONTRACT,
        "ok": True,
        "tenant": {"id": int(tenant.id), "slug": str(tenant.slug)},
        "summary": summarize_survey_response_effects(int(tenant.id)),
    }


@v2_surveys_bp.route("/response-effects", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_response_effects_summary_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    try:
        return _json_response(_response_effect_summary_payload(tenant))
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "[surveys] No se pudo resumir el outbox para tenant %s",
            tenant.id,
        )
        return _error_response(
            "No se pudo obtener el estado de efectos de respuestas",
            500,
            "survey_response_effect_summary_failed",
            "retry_later",
        )


@v2_surveys_bp.route("/response-effects/reconcile", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def reconcile_survey_response_effects_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    limit, limit_error = _response_effect_reconcile_limit()
    if limit_error:
        return limit_error

    try:
        reconciliation = dispatch_survey_response_effects(
            tenant_id=int(tenant.id),
            limit=int(limit),
        )
        return _json_response(
            {
                "contract_version": _RESPONSE_EFFECT_RECONCILE_CONTRACT,
                "ok": True,
                "tenant": {"id": int(tenant.id), "slug": str(tenant.slug)},
                "limit": {
                    "applied": int(limit),
                    "maximum": _MAX_RESPONSE_EFFECT_RECONCILE_LIMIT,
                },
                "reconciliation": reconciliation,
                "summary": summarize_survey_response_effects(int(tenant.id)),
            }
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "[surveys] No se pudo reconciliar el outbox para tenant %s",
            tenant.id,
        )
        return _error_response(
            "No se pudo reconciliar los efectos de respuestas",
            500,
            "survey_response_effect_reconcile_failed",
            "retry_later",
        )


def _normalize_question(question: dict[str, Any], index: int) -> dict[str, Any]:
    q_type = str(question.get("type") or question.get("tipo") or "single").strip().lower()
    internal_type = _TYPE_MAP.get(q_type)
    if internal_type is None:
        is_known_lossy_type = q_type in _LOSSY_UNSUPPORTED_QUESTION_TYPES
        raise EncuestaError(
            f"El tipo de pregunta '{q_type}' no puede materializarse sin perdida de datos",
            status_code=422,
            payload={
                "contract_version": "surveys.question_types.v1",
                "reason_code": "unsupported_survey_question_type",
                "retryable": False,
                "action_hint": "replace_unsupported_question_type",
                "question_index": index,
                "question_type": q_type,
                "known_lossy_type": is_known_lossy_type,
                "supported_types": [
                    "single_choice",
                    "multiple_choice",
                    "free_text",
                    "emoji_rating",
                ],
            },
        )
    conditional_logic = (
        question.get("conditional_logic")
        if "conditional_logic" in question
        else question.get("logica_condicional")
    )

    normalized = {
        "tipo": internal_type,
        "texto": question.get("label") or question.get("texto") or question.get("title") or "",
        "obligatoria": bool(question.get("required") if "required" in question else question.get("obligatoria", False)),
        "orden": question.get("order_index") if question.get("order_index") is not None else question.get("orden", index),
        "opciones": question.get("options") or question.get("opciones") or [],
        "conditional_logic": conditional_logic,
    }
    question_ref = question.get("question_ref")
    logical_ref = question.get("logical_ref")
    if "question_ref" in question:
        normalized["question_ref"] = question_ref
    if "logical_ref" in question:
        normalized["logical_ref"] = logical_ref
    question_id = question.get("id") or question.get("pregunta_id") or question.get("question_id")
    if question_id is not None:
        normalized["id"] = question_id
    for key, aliases in {
        "min_selecciones": ("min_selecciones", "minSelections"),
        "max_selecciones": ("max_selecciones", "maxSelections"),
    }.items():
        source = next((alias for alias in aliases if alias in question), None)
        if source is not None:
            normalized[key] = question[source]
    return normalized


def _normalize_admin_payload(payload: dict[str, Any], *, partial: bool = False) -> dict[str, Any]:
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else payload.get("preguntas")
    normalized_questions = []
    for index, question in enumerate(questions or [], start=1):
        if isinstance(question, dict):
            normalized_questions.append(_normalize_question(question, index))

    channel = str(payload.get("channel") or payload.get("canal") or "web").strip().lower()

    normalized = {
        "document_ref": payload.get("document_ref"),
        "titulo": payload.get("title") or payload.get("titulo"),
        "descripcion": payload.get("description") or payload.get("descripcion"),
        "tipo": payload.get("survey_type") or payload.get("tipo") or "opinion",
        "inicio_at": payload.get("opens_at") or payload.get("inicio_at"),
        "fin_at": payload.get("closes_at") or payload.get("fin_at"),
        "preguntas": normalized_questions,
        "tags": payload.get("tags") or [f"channel:{channel}"],
        "politica_unicidad": payload.get("uniqueness_policy") or payload.get("politica_unicidad") or "anon_id",
        "es_votacion_envivo": bool(payload.get("live_vote") or payload.get("es_votacion_envivo", False)),
        "mostrar_resultados_envivo": bool(
            payload.get("show_live_results") or payload.get("mostrar_resultados_envivo", False)
        ),
        "permitir_comentarios": bool(payload.get("allow_comments") or payload.get("permitir_comentarios", False)),
    }
    structure_guard = payload.get("structure_guard")
    if "expected_structure_revision" in payload:
        normalized["expected_structure_revision"] = payload.get(
            "expected_structure_revision"
        )
    elif isinstance(structure_guard, dict) and "revision" in structure_guard:
        normalized["expected_structure_revision"] = structure_guard.get("revision")

    identity_aliases = {
        "anonimo_permitido": ("anonimo_permitido", "anonimato", "allow_anonymous"),
        "requiere_identidad": ("requiere_identidad", "requiere_datos_contacto"),
    }
    identity_defaults = {
        "anonimo_permitido": True,
        "requiere_identidad": False,
    }
    for target, aliases in identity_aliases.items():
        source = next((key for key in aliases if key in payload), None)
        if source is not None:
            normalized[target] = bool(payload[source])
        elif not partial:
            normalized[target] = identity_defaults[target]

    if partial:
        field_sources = {
            "document_ref": ("document_ref",),
            "titulo": ("title", "titulo"),
            "descripcion": ("description", "descripcion"),
            "tipo": ("survey_type", "tipo"),
            "inicio_at": ("opens_at", "inicio_at"),
            "fin_at": ("closes_at", "fin_at"),
            "preguntas": ("questions", "preguntas"),
            "tags": ("tags", "channel", "canal"),
            "politica_unicidad": ("uniqueness_policy", "politica_unicidad"),
            "es_votacion_envivo": ("live_vote", "es_votacion_envivo"),
            "mostrar_resultados_envivo": ("show_live_results", "mostrar_resultados_envivo"),
            "permitir_comentarios": ("allow_comments", "permitir_comentarios"),
        }
        for target, aliases in field_sources.items():
            if not any(key in payload for key in aliases):
                normalized.pop(target, None)

    return normalized


@v2_surveys_bp.route("/surveys", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def list_surveys_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    estado = (request.args.get("estado") or request.args.get("status") or "").strip() or None
    encuestas = list_encuestas(tenant_id=tenant.id, estado=estado)
    return jsonify(
        {
            "items": [serialize_encuesta(encuesta) for encuesta in encuestas],
            "total": len(encuestas),
            "access": integration_access_payload(tenant),
        }
    )


@v2_surveys_bp.route("/surveys", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def create_survey_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    g.tenant_profile = tenant
    try:
        payload = _normalize_admin_payload(request.get_json(silent=True) or {})
        encuesta = create_encuesta(payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return _encuesta_error_response(exc)

    return jsonify(serialize_encuesta(encuesta)), 201


@v2_surveys_bp.route("/surveys/draft", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def save_survey_draft_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    max_draft_bytes = _survey_draft_max_bytes()
    if request.content_length is not None and request.content_length > max_draft_bytes:
        return _survey_draft_too_large_response(max_draft_bytes)

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response(
            "El borrador debe ser un objeto JSON",
            400,
            "invalid_draft_payload",
            "send_json_object",
        )

    requested_revision, revision_error = _parse_survey_draft_revision(payload.get("revision"))
    if revision_error:
        return revision_error

    raw_schema_version = payload.get("schema_version")
    schema_version = str(raw_schema_version or _DEFAULT_SURVEY_DRAFT_SCHEMA_VERSION).strip()
    if (
        not schema_version
        or len(schema_version) > 32
        or any(not (ch.isalnum() or ch in "_.-:") for ch in schema_version)
    ):
        return _error_response(
            "schema_version debe ser un identificador de hasta 32 caracteres",
            400,
            "invalid_draft_schema_version",
            "send_valid_schema_version",
        )

    payload_idempotency_key = payload.get("idempotency_key")
    if payload_idempotency_key not in (None, "") and str(payload_idempotency_key).strip():
        raw_idempotency_key = payload_idempotency_key
    else:
        raw_idempotency_key = request.headers.get("Idempotency-Key")
    idempotency_key = str(raw_idempotency_key).strip() if raw_idempotency_key not in (None, "") else None
    if idempotency_key and len(idempotency_key) > 128:
        return _error_response(
            "idempotency_key no puede superar 128 caracteres",
            400,
            "invalid_idempotency_key",
            "send_valid_idempotency_key",
        )

    draft_id = payload.get("draft_id") or payload.get("id")
    if not draft_id and idempotency_key:
        draft_id = f"draft_{_stable_id_suffix(idempotency_key)}"
    if not draft_id:
        draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    draft_id = str(draft_id).strip()
    if (
        not draft_id
        or len(draft_id) > 160
        or any(not (ch.isalnum() or ch in "_.-:") for ch in draft_id)
    ):
        return _error_response(
            "draft_id debe contener solo letras, numeros, punto, dos puntos, guion o guion bajo y no superar 160 caracteres",
            400,
            "invalid_draft_id",
            "send_valid_draft_id",
        )

    draft_content = _survey_draft_content(payload)
    payload_hash, canonical_size = _survey_draft_fingerprint(draft_content)
    if canonical_size > max_draft_bytes:
        return _survey_draft_too_large_response(max_draft_bytes)

    idempotency_receipt = None
    if idempotency_key:
        idempotency_receipt = (
            SurveyDraftIdempotency.query.filter_by(
                tenant_id=tenant.id,
                idempotency_key=idempotency_key,
            )
            .with_for_update()
            .first()
        )
        if idempotency_receipt is not None:
            if idempotency_receipt.draft_id != draft_id:
                return _survey_draft_idempotency_conflict_response(
                    draft_id,
                    idempotency_receipt,
                )
            if _survey_draft_receipt_matches(
                idempotency_receipt,
                draft_id=draft_id,
                schema_version=schema_version,
                payload_hash=payload_hash,
            ):
                current = db.session.get(SurveyDraft, idempotency_receipt.survey_draft_id)
                if current is not None and current.tenant_id == tenant.id:
                    return _json_response(
                        _serialize_survey_draft(
                            current,
                            tenant=tenant,
                            idempotency_key=idempotency_key,
                        )
                        )
            else:
                return _survey_draft_idempotency_conflict_response(
                    draft_id,
                    idempotency_receipt,
                )

    draft = (
        SurveyDraft.query.filter_by(tenant_id=tenant.id, draft_id=draft_id)
        .with_for_update()
        .first()
    )

    if draft is not None:
        same_content = draft.schema_version == schema_version and (
            draft.payload_hash == payload_hash or draft.payload == draft_content
        )
        if same_content:
            if idempotency_key and idempotency_receipt is None:
                return _persist_survey_draft_save(
                    draft,
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                    schema_version=schema_version,
                    payload_hash=payload_hash,
                )
            return _json_response(
                _serialize_survey_draft(
                    draft,
                    tenant=tenant,
                    idempotency_key=idempotency_key,
                )
            )
        if requested_revision is None or requested_revision != draft.revision:
            return _survey_draft_conflict_response(draft_id, draft)

        draft.payload = draft_content
        draft.payload_hash = payload_hash
        draft.schema_version = schema_version
        draft.revision += 1
        if idempotency_key:
            draft.idempotency_key = idempotency_key
        return _persist_survey_draft_save(
            draft,
            tenant=tenant,
            idempotency_key=idempotency_key,
            schema_version=schema_version,
            payload_hash=payload_hash,
        )

    if requested_revision not in (None, 0, 1):
        return _survey_draft_conflict_response(draft_id, None)

    draft = SurveyDraft(
        tenant_id=tenant.id,
        draft_id=draft_id,
        idempotency_key=idempotency_key,
        schema_version=schema_version,
        revision=1,
        payload=draft_content,
        payload_hash=payload_hash,
        created_by=getattr(current_user, "id", None),
    )
    return _persist_survey_draft_save(
        draft,
        tenant=tenant,
        idempotency_key=idempotency_key,
        schema_version=schema_version,
        payload_hash=payload_hash,
    )


@v2_surveys_bp.route("/surveys/draft/<string:draft_id>", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def get_survey_draft_v2(current_user, draft_id: str):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    draft = SurveyDraft.query.filter_by(tenant_id=tenant.id, draft_id=draft_id).first()
    if draft is None:
        return _error_response(
            "Borrador no encontrado",
            404,
            "survey_draft_not_found",
            "check_draft_id",
        )
    return _json_response(_serialize_survey_draft(draft, tenant=tenant))


@v2_surveys_bp.route("/surveys/drafts", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def list_survey_drafts_v2(current_user):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    limit = min(_coerce_positive_int(request.args.get("limit"), 20), 100)
    drafts = (
        SurveyDraft.query.filter_by(tenant_id=tenant.id)
        .order_by(SurveyDraft.updated_at.desc(), SurveyDraft.id.desc())
        .limit(limit)
        .all()
    )
    return _json_response(
        {
            "ok": True,
            "persisted": True,
            "contract_version": "surveys.drafts.v2",
            "items": [_serialize_survey_draft(draft, tenant=tenant) for draft in drafts],
            "total": len(drafts),
            "limit": limit,
        }
    )


@v2_surveys_bp.route(
    "/surveys/drafts/<string:draft_id>/materialize",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def materialize_survey_draft_v2(current_user, draft_id: str):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response(
            "La solicitud de materializacion debe ser un objeto JSON",
            400,
            "invalid_materialization_payload",
            "send_json_object",
        )

    expected_revision, revision_error = _parse_materialization_revision(
        payload.get("expected_revision")
    )
    if revision_error:
        return revision_error
    idempotency_key, idempotency_error = _materialization_idempotency_key(payload)
    if idempotency_error:
        return idempotency_error

    g.tenant_profile = tenant
    try:
        result = materialize_survey_draft(
            tenant=tenant,
            draft_id=draft_id,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            user=current_user,
        )
    except SurveyDraftMaterializationError as exc:
        db.session.rollback()
        return _json_response(exc.to_dict(), exc.status_code)
    except EncuestaError as exc:
        db.session.rollback()
        return _encuesta_error_response(exc)

    return _json_response(
        serialize_materialization_result(result),
        200 if result.replayed else 201,
    )


@v2_surveys_bp.route("/surveys/<int:survey_id>", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_detail_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied

    try:
        encuesta = get_encuesta(survey_id, tenant_id=tenant.id)
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


def _governance_error_response(exc):
    return _json_response(exc.to_dict(), exc.status_code)


@v2_surveys_bp.route("/surveys/<int:survey_id>/releases", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def list_survey_governance_releases_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    missing = missing_survey_capabilities(
        current_user, SURVEY_GOVERNANCE_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_governance_capability_error(missing)
    from models import EncEncuesta
    from models_survey_governance import SurveyGovernanceRelease
    from services.survey_governance import (
        release_public_consent_status,
        serialize_release,
    )

    # The parent lookup is tenant-scoped before any release identifier is
    # disclosed, preventing cross-tenant enumeration.
    encuesta = EncEncuesta.query.filter_by(
        id=survey_id, tenant_id=tenant.id
    ).first()
    if encuesta is None:
        return _error_response(
            "Encuesta no encontrada", 404, "survey_not_found", "check_survey_id"
        )
    releases = (
        SurveyGovernanceRelease.query.filter_by(
            tenant_id=tenant.id, survey_id=survey_id
        )
        .order_by(SurveyGovernanceRelease.version_number.desc())
        .all()
    )
    response_count = encuesta.respuestas.count()
    writes_allowed = _survey_writes_allowed(tenant)
    active_release = next(
        (item for item in releases if item.status == "published"), None
    )
    latest_release = releases[0] if releases else None
    serialized_releases = []
    for item in releases:
        serialized = serialize_release(item)
        # These booleans are an explicit UI authority hint only. The mutation
        # endpoints repeat the tenant, capability, state and integrity checks.
        serialized["capabilities"] = {
            "can_publish": bool(
                item.status == "draft"
                and release_public_consent_status(item)["complete"] is True
                and writes_allowed
                and encuesta.estado == "borrador"
                and response_count == 0
            ),
            "can_close": bool(
                item.status == "published" and encuesta.estado == "publicada"
            ),
        }
        serialized_releases.append(serialized)
    return _json_response(
        {
            "ok": True,
            "contract_version": "surveys.governance_releases.v1",
            # Bind the administrative envelope to the same tenant selected by
            # the authenticated request.  The browser reconciles this field
            # before exposing any governance mutation.
            "tenant": {"id": int(tenant.id), "slug": str(tenant.slug)},
            "survey_id": survey_id,
            "survey_state": encuesta.estado,
            "active_release_id": active_release.id if active_release else None,
            "latest_release_id": latest_release.id if latest_release else None,
            "capabilities": {
                "read": True,
                "manage": True,
                "plan_allows_write": bool(writes_allowed),
                "create_release": bool(
                    writes_allowed
                    and encuesta.estado == "borrador"
                    and response_count == 0
                    and not releases
                ),
                "required_for_mutation": SURVEY_GOVERNANCE_MANAGE_CAPABILITY,
            },
            "items": serialized_releases,
            "total": len(releases),
        }
    )


@v2_surveys_bp.route("/surveys/<int:survey_id>/releases", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def create_survey_governance_release_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)
    missing = missing_survey_capabilities(
        current_user, SURVEY_GOVERNANCE_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_governance_capability_error(missing)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error_response(
            "El release debe ser un objeto JSON",
            400,
            "survey_governance_payload_invalid",
            "send_json_object",
        )
    from services.survey_governance import (
        SurveyGovernanceError,
        create_release,
        serialize_release,
    )

    try:
        release, replayed = create_release(
            tenant_id=tenant.id,
            survey_id=survey_id,
            actor_user_id=current_user.id,
            payload=payload,
            idempotency_key=request.headers.get("Idempotency-Key"),
            ip_address=request.remote_addr,
        )
    except SurveyGovernanceError as exc:
        db.session.rollback()
        return _governance_error_response(exc)
    return _json_response(
        serialize_release(release, replayed=replayed), 200 if replayed else 201
    )


@v2_surveys_bp.route(
    "/surveys/<int:survey_id>/releases/<int:release_id>/publish",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def publish_survey_governance_release_v2(
    current_user, survey_id: int, release_id: int
):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)
    missing = missing_survey_capabilities(
        current_user, SURVEY_GOVERNANCE_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_governance_capability_error(missing)
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or set(payload) - {"expected_snapshot_sha256"}:
        return _error_response(
            "La publicación sólo admite expected_snapshot_sha256",
            400,
            "survey_governance_publish_payload_invalid",
            "send_expected_snapshot_only",
        )
    from services.survey_governance import (
        SurveyGovernanceError,
        publish_release,
        serialize_release,
    )

    try:
        release, replayed = publish_release(
            tenant_id=tenant.id,
            survey_id=survey_id,
            release_id=release_id,
            actor_user_id=current_user.id,
            idempotency_key=request.headers.get("Idempotency-Key"),
            expected_snapshot_sha256=payload.get("expected_snapshot_sha256"),
            ip_address=request.remote_addr,
        )
    except SurveyGovernanceError as exc:
        db.session.rollback()
        return _governance_error_response(exc)
    return _json_response(serialize_release(release, replayed=replayed), 200)


@v2_surveys_bp.route(
    "/surveys/<int:survey_id>/releases/<int:release_id>/close",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def close_survey_governance_release_v2(
    current_user, survey_id: int, release_id: int
):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    missing = missing_survey_capabilities(
        current_user, SURVEY_GOVERNANCE_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_governance_capability_error(missing)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) - {"human_review_reference"}:
        return _error_response(
            "El cierre requiere human_review_reference",
            400,
            "survey_governance_close_payload_invalid",
            "provide_human_review_reference",
        )
    from services.survey_governance import (
        SurveyGovernanceError,
        close_release,
        serialize_release,
    )

    try:
        release, replayed = close_release(
            tenant_id=tenant.id,
            survey_id=survey_id,
            release_id=release_id,
            actor_user_id=current_user.id,
            idempotency_key=request.headers.get("Idempotency-Key"),
            human_review_reference=payload.get("human_review_reference"),
            ip_address=request.remote_addr,
        )
    except SurveyGovernanceError as exc:
        db.session.rollback()
        return _governance_error_response(exc)
    return _json_response(serialize_release(release, replayed=replayed), 200)


@v2_surveys_bp.route(
    "/surveys/<int:survey_id>/releases/<int:release_id>/eligibility-grants",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def issue_survey_eligibility_grant_v2(
    current_user, survey_id: int, release_id: int
):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return _no_store_response(error)
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return _no_store_response(denied)
    if not _survey_writes_allowed(tenant):
        return _no_store_response(_survey_plan_required_response(tenant))
    missing = missing_survey_capabilities(
        current_user, SURVEY_ELIGIBILITY_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_eligibility_capability_error(missing)
    scope_error = _survey_eligibility_release_scope_error(
        tenant_id=tenant.id,
        survey_id=survey_id,
        release_id=release_id,
    )
    if scope_error is not None:
        return scope_error
    payload = request.get_json(silent=True)
    allowed_fields = {"subject_ref", "review_reference", "expires_at"}
    if not isinstance(payload, dict) or set(payload) - allowed_fields:
        return _survey_eligibility_json_response(
            {
                "ok": False,
                "contract_version": "surveys.eligibility_grant.v1",
                "reason_code": "survey_eligibility_issue_payload_invalid",
                "retryable": False,
                "action_hint": "send_opaque_subject_review_and_optional_expiry",
                "message": "La emision solo admite referencias opacas y vencimiento.",
            },
            400,
        )
    subject_ref = str(payload.get("subject_ref") or "").strip()
    if not _OPAQUE_ELIGIBILITY_SUBJECT_RE.fullmatch(subject_ref):
        return _survey_eligibility_json_response(
            {
                "ok": False,
                "contract_version": "surveys.eligibility_grant.v1",
                "reason_code": "survey_eligibility_subject_ref_invalid",
                "retryable": False,
                "action_hint": "provide_stable_opaque_subject_ref",
                "message": "subject_ref debe ser una referencia opaca estable; no envies DNI, email ni telefono.",
            },
            422,
        )
    from services.survey_eligibility import (
        SurveyEligibilityError,
        issue_eligibility_grant,
        require_eligibility_gate,
        serialize_issue_receipt,
    )

    try:
        grant, _credential, replayed = issue_eligibility_grant(
            tenant_id=tenant.id,
            survey_id=survey_id,
            release_id=release_id,
            actor_user_id=current_user.id,
            subject_namespace=_ELIGIBILITY_SUBJECT_NAMESPACE_V1,
            subject_ref=subject_ref,
            authority_namespace=_ELIGIBILITY_AUTHORITY_NAMESPACE_V1,
            authority_adapter_version=_ELIGIBILITY_AUTHORITY_ADAPTER_V1,
            review_reference=payload.get("review_reference"),
            expires_at=payload.get("expires_at"),
            idempotency_key=request.headers.get("Idempotency-Key"),
            ip_address=request.remote_addr,
        )
        gate = require_eligibility_gate(current_app.config, tenant_id=tenant.id)
        response_payload = serialize_issue_receipt(
            grant,
            gate=gate,
            replayed=replayed,
        )
    except SurveyEligibilityError as exc:
        db.session.rollback()
        return _survey_eligibility_error_response(exc)
    return _survey_eligibility_json_response(
        response_payload,
        200 if replayed else 201,
    )


@v2_surveys_bp.route(
    "/surveys/<int:survey_id>/releases/<int:release_id>/eligibility-grants/<string:grant_ref>/revoke",
    methods=["POST"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def revoke_survey_eligibility_grant_v2(
    current_user, survey_id: int, release_id: int, grant_ref: str
):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return _no_store_response(error)
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return _no_store_response(denied)
    if not _survey_writes_allowed(tenant):
        return _no_store_response(_survey_plan_required_response(tenant))
    missing = missing_survey_capabilities(
        current_user, SURVEY_ELIGIBILITY_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_eligibility_capability_error(missing)
    scope_error = _survey_eligibility_release_scope_error(
        tenant_id=tenant.id,
        survey_id=survey_id,
        release_id=release_id,
    )
    if scope_error is not None:
        return scope_error
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or set(payload) != {"reason_code"}:
        return _survey_eligibility_json_response(
            {
                "ok": False,
                "contract_version": "surveys.eligibility_grant.v1",
                "reason_code": "survey_eligibility_revoke_payload_invalid",
                "retryable": False,
                "action_hint": "send_supported_reason_code_only",
                "message": "La revocacion requiere solamente reason_code.",
            },
            400,
        )
    from services.survey_eligibility import (
        SurveyEligibilityError,
        revoke_eligibility_grant,
        serialize_revocation_receipt,
    )

    try:
        grant, terminal, replayed = revoke_eligibility_grant(
            tenant_id=tenant.id,
            survey_id=survey_id,
            release_id=release_id,
            grant_ref=grant_ref,
            actor_user_id=current_user.id,
            reason_code=payload.get("reason_code"),
            idempotency_key=request.headers.get("Idempotency-Key"),
            ip_address=request.remote_addr,
        )
        response_payload = serialize_revocation_receipt(
            grant,
            terminal,
            replayed=replayed,
        )
    except SurveyEligibilityError as exc:
        db.session.rollback()
        return _survey_eligibility_error_response(exc)
    return _survey_eligibility_json_response(response_payload, 200)


@v2_surveys_bp.route(
    "/surveys/<int:survey_id>/releases/<int:release_id>/eligibility-summary",
    methods=["GET"],
)
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_eligibility_summary_v2(
    current_user, survey_id: int, release_id: int
):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return _no_store_response(error)
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return _no_store_response(denied)
    missing = missing_survey_capabilities(
        current_user, SURVEY_ELIGIBILITY_MANAGE_CAPABILITY
    )
    if missing:
        return _survey_eligibility_capability_error(missing)
    scope_error = _survey_eligibility_release_scope_error(
        tenant_id=tenant.id,
        survey_id=survey_id,
        release_id=release_id,
    )
    if scope_error is not None:
        return scope_error
    from services.survey_eligibility import (
        SurveyEligibilityError,
        eligibility_aggregate,
    )

    try:
        response_payload = eligibility_aggregate(
            tenant_id=tenant.id,
            survey_id=survey_id,
            release_id=release_id,
        )
    except SurveyEligibilityError as exc:
        db.session.rollback()
        return _survey_eligibility_error_response(exc)
    return _survey_eligibility_json_response(response_payload, 200)


@v2_surveys_bp.route("/surveys/<int:survey_id>", methods=["PATCH"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def update_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    g.tenant_profile = tenant
    try:
        payload = _normalize_admin_payload(
            request.get_json(silent=True) or {},
            partial=True,
        )
        encuesta = update_encuesta(survey_id, payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return _encuesta_error_response(exc)

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/<int:survey_id>/publish", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def publish_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    g.tenant_profile = tenant
    try:
        encuesta, link = publicar_encuesta(survey_id, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    payload = serialize_encuesta(encuesta)
    payload["public_token"] = link.slug_publico
    payload["public_url"] = f"/api/v2/public/surveys/{link.slug_publico}"
    _attach_public_contract(
        payload,
        encuesta,
        link.slug_publico,
        tenant_slug=_tenant_slug_for_resolved_survey(encuesta),
        responses_count=0,
    )
    return jsonify(payload)


@v2_surveys_bp.route("/surveys/<int:survey_id>/close", methods=["POST"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def close_survey_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    if not _survey_writes_allowed(tenant):
        return _survey_plan_required_response(tenant)

    g.tenant_profile = tenant
    try:
        encuesta = cerrar_encuesta(survey_id, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(serialize_encuesta(encuesta))


@v2_surveys_bp.route("/surveys/<int:survey_id>/analytics", methods=["GET"])
@token_requerido
@require_role("admin", "empleado", "super_admin")
def survey_analytics_v2(current_user, survey_id: int):
    tenant, error = _resolve_tenant_or_error(required=True)
    if error:
        return error
    allowed, denied = _enforce_tenant_access(current_user, tenant)
    if not allowed:
        return denied
    missing = missing_survey_capabilities(current_user, SURVEY_PII_READ_CAPABILITY)
    if missing:
        return _survey_capability_error([SURVEY_PII_READ_CAPABILITY], missing)

    try:
        encuesta = get_encuesta(survey_id, tenant_id=tenant.id)
        data = get_dashboard_bundle(encuesta.id, filtros={}, granularity=request.args.get("granularity", "day"))
    except EncuestaError as exc:
        return jsonify(exc.to_dict()), exc.status_code

    return jsonify(data)


@v2_public_surveys_bp.route("/<string:token>", methods=["GET"])
def survey_public_by_token_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    demo_payload = build_demo_public_survey_payload(
        token,
        public_base_url=_public_frontend_base_url(),
    )
    if demo_payload:
        return _json_response(_attach_demo_public_contract(demo_payload, token))

    preferred_tenant_id = tenant.id if tenant is not None else None
    try:
        encuesta = get_public_encuesta(
            token,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=preferred_tenant_id is not None,
        )
    except EncuestaError as exc:
        return _encuesta_error_response(exc)

    payload = serialize_public_encuesta(encuesta, slug_publico=token)
    payload.pop("tenant_id", None)
    _attach_public_contract(
        payload,
        encuesta,
        token,
        tenant_slug=_tenant_slug_for_resolved_survey(encuesta),
        responses_count=_survey_response_count(encuesta),
    )
    return _json_response(payload)


def _build_public_survey_response_ack(
    token: str,
    respuesta,
    *,
    rate_limit: Mapping[str, Any] | None = None,
    security: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    encuesta = getattr(respuesta, "encuesta", None)
    live_results_enabled = bool(getattr(encuesta, "mostrar_resultados_envivo", False))
    tenant_slug = _tenant_slug_for_resolved_survey(encuesta)
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    public_state = _survey_public_state(encuesta) if encuesta is not None else None
    responses_count = _survey_response_count(encuesta) if encuesta is not None else None
    operations = (
        _build_survey_operations_contract(
            encuesta,
            token,
            tenant_slug=tenant_slug,
            live_results_enabled=live_results_enabled,
            public_state=public_state,
            responses_count=responses_count,
        )
        if encuesta is not None
        else None
    )
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=live_results_enabled,
        public_state=public_state,
        responses_count=responses_count,
        operations=operations,
    )
    resolved_security = dict(
        security
        or _survey_security_contract(
            status="receipt_replay"
            if bool(getattr(respuesta, "submission_replayed", False))
            else "not_required",
            reason="durable_submission_receipt"
            if bool(getattr(respuesta, "submission_replayed", False))
            else "anonymous_public_survey",
            retryable=False,
            reset_required=False,
        )
    )
    response_payload: dict[str, Any] = {
        "ok": True,
        "contract_version": "surveys.public_response.v2",
        "persisted": True,
        "replayed": bool(getattr(respuesta, "submission_replayed", False)),
        "respuesta_id": respuesta.id,
        "response_id": respuesta.id,
        "instrument_revision": getattr(respuesta, "instrument_revision", None),
        "public_state": public_state,
        "estado_publico": public_state,
        "links": links,
        "share": _build_share_contract(
            token,
            title=getattr(encuesta, "titulo", None),
            tenant_slug=tenant_slug,
        ),
        "realtime": _build_realtime_contract(
            token,
            tenant_slug=tenant_slug,
            enabled=live_results_enabled,
        ),
        "operations": operations,
        "admin_operations": operations,
        "operational_next_steps": next_steps,
        "next_steps": next_steps["items"],
        "security": resolved_security,
        "ui_actions": [],
    }
    from services.survey_governance import response_governance_contract

    response_payload["governance"] = response_governance_contract(respuesta)
    if rate_limit is not None:
        response_payload["rate_limit"] = {
            "limit": rate_limit["limit"],
            "remaining": rate_limit["remaining"],
            "window_seconds": rate_limit["window_seconds"],
            "reset_after_seconds": rate_limit["reset_after_seconds"],
        }
    receipt_contract = survey_response_receipt_contract(respuesta)
    if receipt_contract is not None:
        response_payload["idempotency"] = receipt_contract
    response_payload["frontend_contract"] = _survey_frontend_security_contract(
        resolved_security,
        can_retry=False,
        reset_turnstile=False,
    )
    if live_results_enabled:
        live_results_url = links["live_results_endpoint"]
        response_payload["live_results_url"] = live_results_url
        response_payload["ui_actions"].append(
            {
                "id": "open_live_results",
                "label": "Ver resultados en vivo",
                "href": live_results_url,
            }
        )
    response_payload["ui_actions"] = _merge_ui_actions(
        response_payload.get("ui_actions"),
        [
            {"id": "share_public_link", "label": "Compartir", "href": links["share_url"]},
            {"id": "download_qr", "label": "QR", "href": links["qr_image_url"]},
        ],
    )
    return response_payload


def _safe_public_survey_response_ack(
    token: str,
    respuesta,
    *,
    rate_limit: Mapping[str, Any] | None = None,
    security: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        return _build_public_survey_response_ack(
            token,
            respuesta,
            rate_limit=rate_limit,
            security=security,
        )
    except Exception:
        current_app.logger.exception(
            "[encuestas] Respuesta %s persistida; fallo el enriquecimiento del ACK",
            getattr(respuesta, "id", None),
        )
        fallback = {
            "ok": True,
            "contract_version": "surveys.public_response.v2",
            "persisted": True,
            "replayed": bool(getattr(respuesta, "submission_replayed", False)),
            "respuesta_id": respuesta.id,
            "response_id": respuesta.id,
            "instrument_revision": getattr(respuesta, "instrument_revision", None),
            "warning": {
                "reason_code": "survey_response_ack_enrichment_failed",
                "message": "La respuesta fue guardada; algunos enlaces no estan disponibles temporalmente.",
            },
        }
        from services.survey_governance import response_governance_contract

        fallback["governance"] = response_governance_contract(respuesta)
        receipt_contract = survey_response_receipt_contract(respuesta)
        if receipt_contract is not None:
            fallback["idempotency"] = receipt_contract
        return fallback


@v2_public_surveys_bp.route("/<string:token>/respond", methods=["POST"])
def respond_public_survey_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    try:
        submission_id = resolve_survey_submission_id(
            payload,
            header_value=request.headers.get("Idempotency-Key"),
            # Synthetic demo surveys never write to the database. Every
            # canonical HTTP submission must carry caller-owned replay
            # identity before rate limiting, Turnstile, or persistence.
            required=not is_demo_survey_slug(token),
        )
    except EncuestaError as exc:
        return _encuesta_error_response(exc)
    client_ip = _public_client_ip()
    request_ctx = {
        "ip": client_ip,
        "user_agent": request.headers.get("User-Agent"),
        "referer": request.headers.get("Referer"),
        "anon_id": _public_anon_id(payload),
        "canal": payload.get("source") or payload.get("channel") or payload.get("canal") or "public_link",
    }
    preferred_tenant_id = tenant.id if tenant is not None else None
    authenticated_user = None
    authenticated_user_resolved = False
    if submission_id is not None and not is_demo_survey_slug(token):
        try:
            authenticated_user = resolve_optional_survey_bearer_user(
                request.headers.get("Authorization")
            )
            authenticated_user_resolved = True
            replay = find_survey_response_replay(
                token,
                payload,
                request_ctx,
                submission_id=submission_id,
                preferred_tenant_id=preferred_tenant_id,
                require_tenant_match=preferred_tenant_id is not None,
                authenticated_user=authenticated_user,
            )
        except EncuestaError as exc:
            db.session.rollback()
            return _encuesta_error_response(exc)
        if replay is not None:
            replay_scope = enforce_public_survey_replay_scope(
                replay,
                preferred_tenant_id=preferred_tenant_id,
            )
            if replay_scope is not None:
                return _json_response(
                    replay_scope.error_payload(),
                    replay_scope.status_code or 404,
                )
            replay_security = _survey_security_contract(
                status="receipt_replay",
                reason="durable_submission_receipt",
                retryable=False,
                reset_required=False,
            )
            return _json_response(
                _safe_public_survey_response_ack(
                    token,
                    replay,
                    security=replay_security,
                ),
                200,
            )

    intake_decision = enforce_public_survey_intake(
        token,
        payload,
        preferred_tenant_id=preferred_tenant_id,
        request_id=_request_id(),
        synthetic=is_demo_survey_slug(token),
        verifier=verify_turnstile,
    )
    rate_limit = intake_decision.rate_limit
    if not intake_decision.allowed:
        response = _json_response(
            intake_decision.error_payload(),
            intake_decision.status_code or 503,
        )
        return _attach_rate_limit_headers(response, rate_limit)
    security = intake_decision.security or _survey_security_contract(
        status="not_required",
        reason="anonymous_public_survey",
        retryable=False,
        reset_required=False,
    )

    demo_ack = build_demo_survey_response_ack(
        token,
        payload,
        public_base_url=_public_frontend_base_url(),
    )
    if demo_ack:
        response = _json_response(
            _attach_demo_response_contract(demo_ack, token, security=security),
            201,
        )
        return _attach_rate_limit_headers(response, rate_limit)

    try:
        if not authenticated_user_resolved:
            authenticated_user = resolve_optional_survey_bearer_user(
                request.headers.get("Authorization")
            )
        respuesta = save_respuesta(
            token,
            payload,
            request_ctx,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=preferred_tenant_id is not None,
            authenticated_user=authenticated_user,
            submission_id=submission_id,
            eligibility_credential=request.headers.get(
                SURVEY_ELIGIBILITY_CREDENTIAL_HEADER
            ),
            eligibility_transport="http",
        )
    except EncuestaError as exc:
        db.session.rollback()
        return _encuesta_error_response(exc)
    except Exception:
        db.session.rollback()
        raise

    response_payload = _safe_public_survey_response_ack(
        token,
        respuesta,
        rate_limit=rate_limit,
        security=security,
    )

    response_status = 200 if response_payload["replayed"] else 201
    response = _json_response(response_payload, response_status)
    return _attach_rate_limit_headers(response, rate_limit)


@v2_public_surveys_bp.route("/<string:token>/live-results", methods=["GET"])
def survey_live_results_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    include_heatmap = str(request.args.get("include_heatmap", "1")).strip().lower() not in {"0", "false", "no", "off"}
    max_points = request.args.get("max_points", default=2000, type=int) or 2000
    max_cells = request.args.get("max_cells", default=200, type=int) or 200
    momentum_window_minutes = request.args.get("momentum_window_minutes", type=int)
    if momentum_window_minutes is None:
        momentum_window_minutes = request.args.get("window_minutes", default=10, type=int) or 10
    filtros = {
        key: value
        for key in (
            "range_preset",
            "range_timezone",
            "desde",
            "hasta",
            "canal",
            "barrio",
            "ciudad",
            "provincia",
        )
        if (value := (request.args.get(key) or "").strip())
    }
    preferred_tenant_id = tenant.id if tenant is not None else None

    demo_results = build_demo_live_results_payload(
        token,
        public_base_url=_public_frontend_base_url(),
    )
    if demo_results:
        return _json_response(_attach_demo_live_results_contract(demo_results, token))

    try:
        encuesta = get_public_encuesta(
            token,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=preferred_tenant_id is not None,
        )
        if not bool(getattr(encuesta, "mostrar_resultados_envivo", False)):
            return _error_response(
                "Los resultados en vivo no estan publicados para esta encuesta.",
                403,
                "live_results_hidden",
                "wait_for_results_publication",
            )
        results = calculate_live_results(
            token,
            preferred_tenant_id=preferred_tenant_id,
            require_tenant_match=preferred_tenant_id is not None,
            include_heatmap=include_heatmap,
            max_points=max(100, min(max_points, 5000)),
            max_cells=max(50, min(max_cells, 1000)),
            momentum_window_minutes=momentum_window_minutes,
            filtros=filtros,
            geo_privacy="public_aggregated",
        )
    except EncuestaError as exc:
        return _encuesta_error_response(exc)

    results.setdefault("contract_version", "surveys.live_results.v2")
    results.setdefault("slug_publico", token)
    results.setdefault(
        "render_contract",
        {
            "preferred_visualization": "live_vote_dashboard",
            "supports": ["cards", "bars", "timeline", "heatmap", "map_pulses"],
            "polling_interval_ms": 8000,
            "empty_state": "Todavia no hay respuestas para mostrar.",
        },
    )
    _attach_live_results_contract(
        results,
        encuesta,
        token,
        tenant_slug=_tenant_slug_for_resolved_survey(encuesta),
    )
    return _json_response(results)
