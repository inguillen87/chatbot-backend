from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone
import os
from threading import Lock
import time
from typing import Any
from urllib.parse import quote_plus, urlencode
import uuid

from flask import Blueprint, current_app, g, jsonify, request

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.encuestas_analytics_service import get_dashboard_bundle
from services.encuestas_analytics_service import calculate_live_results
from services.encuestas_service import (
    EncuestaError,
    cerrar_encuesta,
    create_encuesta,
    get_encuesta,
    get_public_encuesta,
    list_encuestas,
    publicar_encuesta,
    save_respuesta,
    serialize_encuesta,
    serialize_public_encuesta,
    update_encuesta,
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_full_integrations,
)
from utils.auth_helpers import token_requerido
from utils.permissions import require_role

v2_surveys_bp = Blueprint("v2_surveys", __name__, url_prefix="/api/v2")
v2_public_surveys_bp = Blueprint("v2_public_surveys", __name__, url_prefix="/api/v2/public/surveys")

_DEFAULT_PUBLIC_RESPONSE_RATE_LIMIT = 150
_DEFAULT_PUBLIC_RESPONSE_RATE_PERIOD = 60
_public_response_rate_buckets: defaultdict[str, deque[float]] = defaultdict(deque)
_public_response_rate_lock = Lock()

_TYPE_MAP = {
    "single": "opcion_unica",
    "multi": "opcion_multiple",
    "rating": "rating_emoji",
    "text": "abierta",
    "nps": "rating_emoji",
    "ranking": "opcion_multiple",
    "location": "abierta",
}


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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or "0.0.0.0"
    return request.remote_addr or "0.0.0.0"


def _public_response_rate_settings() -> tuple[int, int]:
    limit = current_app.config.get("PUBLIC_ENCUESTAS_RATE_LIMIT")
    period = current_app.config.get("PUBLIC_ENCUESTAS_RATE_PERIOD")
    if limit is None:
        limit = os.getenv("PUBLIC_ENCUESTAS_RATE_LIMIT")
    if period is None:
        period = os.getenv("PUBLIC_ENCUESTAS_RATE_PERIOD")
    return (
        _coerce_positive_int(limit, _DEFAULT_PUBLIC_RESPONSE_RATE_LIMIT),
        _coerce_positive_int(period, _DEFAULT_PUBLIC_RESPONSE_RATE_PERIOD),
    )


def _public_response_rate_limit(token: str) -> dict[str, Any]:
    limit, period = _public_response_rate_settings()
    now = time.time()
    key = f"{token}:{_public_client_ip()}"
    with _public_response_rate_lock:
        bucket = _public_response_rate_buckets[key]
        while bucket and now - bucket[0] > period:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(period - (now - bucket[0]) + 0.999))
            return {
                "allowed": False,
                "limit": limit,
                "remaining": 0,
                "window_seconds": period,
                "retry_after_seconds": retry_after,
                "reset_after_seconds": retry_after,
            }

        bucket.append(now)
        reset_after = max(1, int(period - (now - bucket[0]) + 0.999))
        return {
            "allowed": True,
            "limit": limit,
            "remaining": max(0, limit - len(bucket)),
            "window_seconds": period,
            "retry_after_seconds": 0,
            "reset_after_seconds": reset_after,
        }


def _attach_rate_limit_headers(response, telemetry: dict[str, Any]):
    response.headers["X-RateLimit-Limit"] = str(telemetry.get("limit", ""))
    response.headers["X-RateLimit-Remaining"] = str(telemetry.get("remaining", ""))
    response.headers["X-RateLimit-Window"] = str(telemetry.get("window_seconds", ""))
    if not telemetry.get("allowed", True):
        response.headers["Retry-After"] = str(telemetry.get("retry_after_seconds", 1))
    return response


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
    return plan_allows_full_integrations(tenant)


def _stable_id_suffix(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isalnum() or ch in {"_", "-"}).strip("_-")
    return text[:48] or uuid.uuid4().hex[:12]


def _resolve_tenant_or_error(*, required: bool = True):
    explicit_slug = (
        request.headers.get("X-Tenant-Slug")
        or request.headers.get("X-Tenant")
        or request.args.get("tenant_slug")
        or request.args.get("tenant")
        or ""
    ).strip()
    if required and not explicit_slug:
        return None, _error_response("X-Tenant-Slug es obligatorio en surveys v2", 400, "missing_tenant", "send_tenant_slug")
    try:
        return resolve_tenant_v2(required=required, explicit_slug=explicit_slug or None), None
    except V2TenantResolutionError as exc:
        return None, _error_response(exc.message, exc.status_code, "tenant_resolution_failed", "check_tenant_slug")


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
    room = f"encuesta_{token}"
    live_results_endpoint = _build_survey_links(token, tenant_slug=tenant_slug)["live_results_endpoint"]
    return {
        "contract_version": "surveys.realtime.v2",
        "enabled": bool(enabled),
        "transports": ["socket.io", "polling"] if enabled else [],
        "room": room if enabled else None,
        "socket": {
            "enabled": bool(enabled),
            "path": "/api/socket.io",
            "join_event": "join",
            "join_payload": {"room": room},
            "events": [
                {"name": "survey_update_v2", "contract_version": "surveys.live_results.v2"},
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


def _build_operational_next_steps(
    token: str,
    *,
    tenant_slug: str | None = None,
    live_results_enabled: bool,
    public_state: dict[str, Any] | None = None,
    responses_count: int | None = None,
) -> dict[str, Any]:
    links = _build_survey_links(token, tenant_slug=tenant_slug)
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
        items.extend(
            [
                {
                    "id": "open_live_results",
                    "label": "Abrir resultados en vivo",
                    "href": links["live_results_endpoint"],
                    "priority": 3,
                },
                {
                    "id": "subscribe_realtime_room",
                    "label": "Suscribirse a realtime",
                    "room": f"encuesta_{token}",
                    "event": "survey_update_v2",
                    "priority": 4,
                },
            ]
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
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=live_results_enabled,
        public_state=public_state,
        responses_count=responses_count,
    )

    payload.setdefault("contract_version", "surveys.public.v2")
    payload["public_state"] = public_state
    payload["estado_publico"] = public_state
    payload["links"] = {**(payload.get("links") or {}), **links}
    payload["share"] = _build_share_contract(token, title=title, tenant_slug=tenant_slug)
    payload["realtime"] = realtime
    payload["operational_next_steps"] = next_steps
    payload["next_steps"] = next_steps["items"]
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
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=True,
        public_state=public_state,
        responses_count=int(results.get("total_respuestas") or 0),
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
    results["operational_next_steps"] = next_steps
    results["next_steps"] = next_steps["items"]

    render_contract = results.setdefault("render_contract", {})
    supports = list(render_contract.get("supports") or [])
    for capability in ("realtime_socket", "polling_fallback", "qr_share", "admin_next_steps"):
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
    values = {
        getattr(current_user, "tenant_id", None),
        getattr(current_user, "municipio_id", None),
        getattr(current_user, "empresa_id", None),
        getattr(current_user, "pyme_id", None),
    }
    normalized: set[int] = set()
    for value in values:
        if value in (None, ""):
            continue
        try:
            normalized.add(int(value))
        except (TypeError, ValueError):
            continue
    return normalized


def _enforce_tenant_access(current_user, tenant) -> tuple[bool, tuple | None]:
    role = str(getattr(current_user, "rol", "") or "").lower()
    if role == "super_admin":
        # super_admin is allowed only when tenant was explicitly resolved by v2 resolver
        return True, None

    candidates = _user_tenant_candidates(current_user)
    if tenant.id in candidates:
        return True, None

    if (getattr(current_user, "tenant_slug", None) or "").strip().lower() == (tenant.slug or "").strip().lower():
        return True, None

    return False, _error_response("Permisos insuficientes para este tenant", 403, "forbidden_tenant", "switch_tenant")


def _normalize_question(question: dict[str, Any], index: int) -> dict[str, Any]:
    q_type = str(question.get("type") or question.get("tipo") or "single").strip().lower()
    internal_type = _TYPE_MAP.get(q_type, q_type)

    return {
        "tipo": internal_type,
        "texto": question.get("label") or question.get("texto") or question.get("title") or "",
        "obligatoria": bool(question.get("required") if "required" in question else question.get("obligatoria", False)),
        "orden": question.get("order_index") if question.get("order_index") is not None else question.get("orden", index),
        "opciones": question.get("options") or question.get("opciones") or [],
        "logica_condicional": question.get("conditional_logic") or question.get("logica_condicional"),
    }


def _normalize_admin_payload(payload: dict[str, Any]) -> dict[str, Any]:
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else payload.get("preguntas")
    normalized_questions = []
    for index, question in enumerate(questions or [], start=1):
        if isinstance(question, dict):
            normalized_questions.append(_normalize_question(question, index))

    channel = str(payload.get("channel") or payload.get("canal") or "web").strip().lower()

    return {
        "titulo": payload.get("title") or payload.get("titulo"),
        "descripcion": payload.get("description") or payload.get("descripcion"),
        "tipo": payload.get("survey_type") or payload.get("tipo") or "opinion",
        "inicio_at": payload.get("opens_at") or payload.get("inicio_at"),
        "fin_at": payload.get("closes_at") or payload.get("fin_at"),
        "preguntas": normalized_questions,
        "tags": payload.get("tags") or [f"channel:{channel}"],
        "anonimo_permitido": bool(payload.get("allow_anonymous", True)),
        "politica_unicidad": payload.get("uniqueness_policy") or payload.get("politica_unicidad") or "anon_id",
        "es_votacion_envivo": bool(payload.get("live_vote") or payload.get("es_votacion_envivo", False)),
        "mostrar_resultados_envivo": bool(
            payload.get("show_live_results") or payload.get("mostrar_resultados_envivo", False)
        ),
        "permitir_comentarios": bool(payload.get("allow_comments") or payload.get("permitir_comentarios", False)),
    }


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

    payload = _normalize_admin_payload(request.get_json(silent=True) or {})
    g.tenant_profile = tenant
    try:
        encuesta = create_encuesta(payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

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

    payload = request.get_json(silent=True) or {}
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    normalized_questions = []
    for index, question in enumerate(questions, start=1):
        if not isinstance(question, dict):
            continue
        normalized_questions.append(
            {
                "id": question.get("id") or f"question-{index}",
                "title": question.get("title") or question.get("label") or question.get("texto") or "",
                "type": question.get("type") or question.get("tipo") or "single",
                "required": bool(question.get("required", False)),
                "options": question.get("options") or question.get("opciones") or [],
            }
        )

    idempotency_key = payload.get("idempotency_key") or request.headers.get("Idempotency-Key")
    draft_id = payload.get("draft_id") or payload.get("id")
    if not draft_id and idempotency_key:
        draft_id = f"draft_{_stable_id_suffix(idempotency_key)}"
    if not draft_id:
        draft_id = f"draft_{uuid.uuid4().hex[:12]}"
    return _json_response(
        {
            "ok": True,
            "contract_version": "surveys.draft.v2",
            "draft_id": str(draft_id),
            "status": "draft",
            "idempotency_key": idempotency_key,
            "tenant": {"id": tenant.id, "slug": tenant.slug},
            "draft": {
                "title": payload.get("title") or payload.get("titulo") or "",
                "description": payload.get("description") or payload.get("descripcion") or "",
                "questions": normalized_questions,
            },
        }
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

    payload = _normalize_admin_payload(request.get_json(silent=True) or {})
    g.tenant_profile = tenant
    try:
        encuesta = update_encuesta(survey_id, payload, current_user)
    except EncuestaError as exc:
        db.session.rollback()
        return jsonify(exc.to_dict()), exc.status_code

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
        tenant_slug=_tenant_slug_value(tenant),
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

    preferred_tenant_id = tenant.id if tenant is not None else None
    try:
        encuesta = get_public_encuesta(token, preferred_tenant_id=preferred_tenant_id)
    except EncuestaError as exc:
        return _encuesta_error_response(exc)

    payload = serialize_public_encuesta(encuesta, slug_publico=token)
    payload.pop("tenant_id", None)
    _attach_public_contract(
        payload,
        encuesta,
        token,
        tenant_slug=_tenant_slug_value(tenant),
        responses_count=_survey_response_count(encuesta),
    )
    return _json_response(payload)


@v2_public_surveys_bp.route("/<string:token>/respond", methods=["POST"])
def respond_public_survey_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    payload = request.get_json(silent=True) or {}
    client_ip = _public_client_ip()
    request_ctx = {
        "ip": client_ip,
        "user_agent": request.headers.get("User-Agent"),
        "referer": request.headers.get("Referer"),
        "anon_id": payload.get("anon_id") or payload.get("anonId") or request.cookies.get("anon_id"),
        "canal": payload.get("source") or payload.get("channel") or payload.get("canal") or "public_link",
    }
    preferred_tenant_id = tenant.id if tenant is not None else None
    rate_limit = _public_response_rate_limit(token)
    if not rate_limit["allowed"]:
        response = _json_response(
            {
                "contract_version": "surveys.public_response.v2",
                "ok": False,
                "status_code": 429,
                "reason_code": "rate_limited",
                "retryable": False,
                "action_hint": "retry_later",
                "message": "Demasiadas respuestas desde esta IP. Intenta mas tarde.",
                "error": {"code": 429, "message": "Demasiadas respuestas desde esta IP. Intenta mas tarde."},
                "rate_limit": {
                    "limit": rate_limit["limit"],
                    "remaining": rate_limit["remaining"],
                    "window_seconds": rate_limit["window_seconds"],
                    "retry_after_seconds": rate_limit["retry_after_seconds"],
                },
            },
            429,
        )
        return _attach_rate_limit_headers(response, rate_limit)

    try:
        respuesta = save_respuesta(token, payload, request_ctx, preferred_tenant_id=preferred_tenant_id)
        db.session.commit()
    except EncuestaError as exc:
        db.session.rollback()
        return _encuesta_error_response(exc)
    except Exception:
        db.session.rollback()
        raise

    encuesta = getattr(respuesta, "encuesta", None)
    live_results_enabled = bool(getattr(encuesta, "mostrar_resultados_envivo", False))
    tenant_slug = _tenant_slug_value(tenant)
    links = _build_survey_links(token, tenant_slug=tenant_slug)
    public_state = _survey_public_state(encuesta) if encuesta is not None else None
    next_steps = _build_operational_next_steps(
        token,
        tenant_slug=tenant_slug,
        live_results_enabled=live_results_enabled,
        public_state=public_state,
    )
    response_payload = {
        "ok": True,
        "contract_version": "surveys.public_response.v2",
        "respuesta_id": respuesta.id,
        "response_id": respuesta.id,
        "public_state": public_state,
        "estado_publico": public_state,
        "links": links,
        "share": _build_share_contract(
            token,
            title=getattr(encuesta, "titulo", None),
            tenant_slug=tenant_slug,
        ),
        "realtime": _build_realtime_contract(token, tenant_slug=tenant_slug, enabled=live_results_enabled),
        "operational_next_steps": next_steps,
        "next_steps": next_steps["items"],
        "rate_limit": {
            "limit": rate_limit["limit"],
            "remaining": rate_limit["remaining"],
            "window_seconds": rate_limit["window_seconds"],
            "reset_after_seconds": rate_limit["reset_after_seconds"],
        },
        "ui_actions": [],
    }
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

    response = _json_response(response_payload, 201)
    return _attach_rate_limit_headers(response, rate_limit)


@v2_public_surveys_bp.route("/<string:token>/live-results", methods=["GET"])
def survey_live_results_v2(token: str):
    tenant, error = _resolve_tenant_or_error(required=False)
    if error:
        return error

    include_heatmap = str(request.args.get("include_heatmap", "1")).strip().lower() not in {"0", "false", "no", "off"}
    max_points = request.args.get("max_points", default=2000, type=int) or 2000
    max_cells = request.args.get("max_cells", default=200, type=int) or 200
    window_minutes = request.args.get("window_minutes", default=10, type=int) or 10
    filtros = {
        key: value
        for key in ("canal", "barrio", "ciudad", "provincia")
        if (value := (request.args.get(key) or "").strip())
    }
    preferred_tenant_id = tenant.id if tenant is not None else None

    try:
        encuesta = get_public_encuesta(token, preferred_tenant_id=preferred_tenant_id)
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
            include_heatmap=include_heatmap,
            max_points=max(100, min(max_points, 5000)),
            max_cells=max(50, min(max_cells, 1000)),
            momentum_window_minutes=max(5, min(window_minutes, 30)),
            filtros=filtros,
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
    _attach_live_results_contract(results, encuesta, token, tenant_slug=_tenant_slug_value(tenant))
    return _json_response(results)
