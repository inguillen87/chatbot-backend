"""Public endpoints for answering and listing active surveys."""
from __future__ import annotations

import io
import json
import os
import uuid
import re
from typing import Any, Dict, Iterator, Mapping, Optional, Pattern, Sequence, Union

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
)
from flask_login import current_user
from urllib.parse import quote_plus

from config import (
    ALLOWED_ORIGINS as DEFAULT_ALLOWED_ORIGINS,
    ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH,
)
from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_qr_service import build_qr_png
from services.encuestas_service import (
    EncuestaError,
    find_survey_response_replay,
    get_public_encuesta,
    list_public_encuestas_for_tenant,
    save_respuesta,
    serialize_public_encuesta,
    serialize_public_comment,
    create_comentario,
    list_comentarios,
    reportar_comentario,
    resolve_optional_survey_bearer_user,
    resolve_survey_submission_id,
    survey_response_receipt_contract,
    verify_social_comment_token,
)
from services.encuestas_analytics_service import calculate_live_results
from services.demo_surveys import (
    build_demo_live_results_payload,
    build_demo_public_survey_payload,
    build_demo_survey_response_ack,
    build_demo_surveys_votings_contract,
    is_demo_survey_slug,
)
from services.public_survey_intake import (
    attach_public_survey_rate_limit_headers,
    enforce_public_survey_intake,
    enforce_public_survey_replay_scope,
    public_survey_client_ip,
)
from models import TenantProfile
from utils.auth_helpers import obtener_token, user_from_token

ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION = "encuestas.public_response.v1"

AllowedOrigin = Union[str, Pattern[str]]


def _safe_json_loads(raw: Optional[str]) -> Optional[Any]:
    """Best-effort JSON parsing that never raises."""

    if not isinstance(raw, str):
        return None

    candidate = raw.strip()
    if not candidate:
        return None

    try:
        return json.loads(candidate)
    except (TypeError, ValueError):
        return None


def _coerce_form_scalar(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    parsed = _safe_json_loads(value)
    if parsed is not None:
        return parsed
    stripped = value.strip()
    return stripped or value


def _assign_nested_form_values(
    container: Dict[str, Any],
    path: Sequence[str],
    raw_values: Sequence[str],
    is_list: bool,
) -> None:
    if not path:
        return

    key = path[0]
    if len(path) == 1:
        coerced = [_coerce_form_scalar(item) for item in raw_values]
        if is_list or len(coerced) > 1:
            container[key] = coerced
        elif coerced:
            container[key] = coerced[0]
        return

    next_container = container.get(key)
    if not isinstance(next_container, dict):
        next_container = {}
        container[key] = next_container
    _assign_nested_form_values(next_container, path[1:], raw_values, is_list)


def _payload_from_form(form) -> Optional[Dict[str, Any]]:
    """Normalize form-encoded submissions into a JSON-like payload."""

    if not form:
        return None

    payload: Dict[str, Any] = {}
    respuestas_nested: Dict[int, Dict[str, Any]] = {}
    metadata_nested: Dict[str, Any] = {}

    for key, values in form.lists():
        if not values:
            continue

        parts = re.findall(r"([^\[\]]+)", key)
        is_list = key.endswith("[]") or len(values) > 1

        if parts and parts[0] == "respuestas" and len(parts) >= 3:
            index_part = parts[1]
            if index_part.isdigit():
                idx = int(index_part)
                target = respuestas_nested.setdefault(idx, {})
                _assign_nested_form_values(target, parts[2:], values, is_list)
                continue

        if parts and parts[0] == "metadata" and len(parts) >= 2:
            _assign_nested_form_values(metadata_nested, parts[1:], values, is_list)
            continue

        payload[key] = (
            _coerce_form_scalar(values[0])
            if len(values) == 1
            else [_coerce_form_scalar(item) for item in values]
        )

    if metadata_nested:
        existing = payload.get("metadata")
        if isinstance(existing, dict):
            existing.update(metadata_nested)
        else:
            payload["metadata"] = metadata_nested

    if respuestas_nested:
        ordered = [respuestas_nested[idx] for idx in sorted(respuestas_nested)]
        payload["respuestas"] = ordered

    raw_embedded = payload.get("payload")
    if isinstance(raw_embedded, str):
        parsed = _safe_json_loads(raw_embedded)
        if isinstance(parsed, dict):
            payload.pop("payload", None)
            for key, value in parsed.items():
                if key in payload and key in {"metadata", "respuestas"}:
                    continue
                payload[key] = value
    elif isinstance(raw_embedded, Mapping):
        payload.pop("payload", None)
        for key, value in raw_embedded.items():
            if key in payload and key in {"metadata", "respuestas"}:
                continue
            payload[key] = value

    for key in ("respuestas", "metadata"):
        value = payload.get(key)
        if isinstance(value, str):
            parsed = _safe_json_loads(value)
            if parsed is not None:
                payload[key] = parsed

    return payload or None


def _extract_request_payload() -> Dict[str, Any]:
    """Obtain the submission payload regardless of encoding."""

    json_payload = request.get_json(silent=True)
    if isinstance(json_payload, dict):
        return json_payload

    form_payload = _payload_from_form(request.form)
    if isinstance(form_payload, dict):
        return form_payload

    raw_body = request.get_data(cache=False, as_text=True)
    parsed_body = _safe_json_loads(raw_body)
    if isinstance(parsed_body, dict):
        return parsed_body

    return {}


def _normalize_host(value: Optional[str]) -> Optional[str]:
    """Normalize host/header values for domain mapping lookups."""

    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    text = re.sub(r"^[a-zA-Z]+://", "", text)
    text = text.split("/")[0]
    text = text.split(",")[-1]
    text = text.split(":")[0]
    text = text.strip().lower()
    return text or None


def _feature_guard():
    if not FEATURE_ENCUESTAS:
        return jsonify({"error": "Módulo de encuestas deshabilitado"}), 404
    return None


def _resolve_preview_user():
    """Return an authenticated user allowed to preview unpublished surveys."""

    token = obtener_token()
    if token:
        user = user_from_token(token)
        if user is not None:
            return user

    try:
        if getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:  # pragma: no cover - extremely defensive
        current_app.logger.exception("[encuestas] Failed to resolve preview user from session")

    return None


def _public_base_url() -> str:
    configured = current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return request.host_url.rstrip("/")


def _public_api_base_url() -> str:
    api_base = current_app.config.get("PUBLIC_ENCUESTAS_API_BASE_URL")
    if isinstance(api_base, str) and api_base.strip():
        return api_base.rstrip("/")

    backend = current_app.config.get("BACKEND_URL")
    if isinstance(backend, str) and backend.strip():
        return backend.rstrip("/")

    return request.host_url.rstrip("/")


def _public_target_base_url() -> str:
    target = current_app.config.get("PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL")
    if isinstance(target, str) and target.strip():
        return target.rstrip("/")

    canonical = current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    if isinstance(canonical, str) and canonical.strip():
        return canonical.rstrip("/")

    return request.host_url.rstrip("/")


def _isoformat_or_none(value: Any) -> Optional[str]:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return None


def _serialize_public_encuesta_summary(
    encuesta: Any,
    slug_publico: str,
    base_url: str,
) -> Dict[str, Any]:
    """Build a lightweight list item without loading questions or live results."""

    if isinstance(encuesta, Mapping):
        data = dict(encuesta)
        slug = slug_publico or data.get("slug")
        data["slug"] = slug
        data.setdefault("slug_publico", slug)
        data.setdefault("contract_version", "encuestas.public.v1")
        data["url_publica"] = f"{base_url}/e/{slug}"
        return data

    slug = slug_publico or getattr(encuesta, "slug", None)
    return {
        "contract_version": "encuestas.public.v1",
        "id": getattr(encuesta, "id", None),
        "tenant_id": getattr(encuesta, "tenant_id", None),
        "slug": slug,
        "slug_publico": slug,
        "titulo": getattr(encuesta, "titulo", None),
        "descripcion": getattr(encuesta, "descripcion", None),
        "tipo": getattr(encuesta, "tipo", None),
        "inicio_at": _isoformat_or_none(getattr(encuesta, "inicio_at", None)),
        "fin_at": _isoformat_or_none(getattr(encuesta, "fin_at", None)),
        "created_at": _isoformat_or_none(getattr(encuesta, "created_at", None)),
        "updated_at": _isoformat_or_none(getattr(encuesta, "updated_at", None)),
        "es_votacion_envivo": bool(getattr(encuesta, "es_votacion_envivo", False)),
        "mostrar_resultados_envivo": bool(getattr(encuesta, "mostrar_resultados_envivo", False)),
        "permitir_comentarios": bool(getattr(encuesta, "permitir_comentarios", False)),
        "url_publica": f"{base_url}/e/{slug}",
    }


def _resolve_comment_social_providers() -> list[dict]:
    configured = current_app.config.get("SURVEY_COMMENT_SOCIAL_PROVIDERS")
    if isinstance(configured, list) and configured:
        normalized = []
        for item in configured:
            if isinstance(item, dict):
                provider_id = str(item.get("id") or item.get("provider") or "").strip().lower()
                if provider_id:
                    normalized.append(
                        {
                            "id": provider_id,
                            "label": str(item.get("label") or provider_id.title()),
                        }
                    )
            elif isinstance(item, str) and item.strip():
                provider_id = item.strip().lower()
                normalized.append({"id": provider_id, "label": provider_id.title()})
        if normalized:
            return normalized

    return [
        {"id": "facebook", "label": "Facebook"},
        {"id": "google", "label": "Google"},
        {"id": "instagram", "label": "Instagram"},
    ]


def _attach_comment_social_config(data: dict) -> dict:
    if not isinstance(data, dict):
        return data
    if data.get("permitir_comentarios"):
        comment_cfg = data.setdefault("commentConfig", {})
        comment_cfg.setdefault(
            "requiresSocialToken",
            _coerce_bool(current_app.config.get("SURVEY_SOCIAL_COMMENT_REQUIRE_TOKEN"), default=False),
        )
        comment_cfg.setdefault("acceptedModes", ["anon", "social"])
        data.setdefault("socialProviders", _resolve_comment_social_providers())
    return data


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _demo_survey_list_contract_from_request() -> Optional[dict]:
    demo_mode = _coerce_bool(request.args.get("demo_mode"), default=False)
    tenant_slug = (
        request.args.get("tenant_slug")
        or request.args.get("tenant")
        or request.args.get("tenant_ref")
    )
    sector = request.args.get("sector") or request.args.get("demo_sector")
    if not demo_mode or not tenant_slug:
        return None
    return build_demo_surveys_votings_contract(
        sector=sector or "empresas",
        tenant_slug=tenant_slug,
        public_base_url=_public_target_base_url(),
        page=request.args.get("page", default=1, type=int) or 1,
        page_size=request.args.get("limit", default=5, type=int) or 5,
    )


def _enrich_comment_payload_with_social_token(payload: Dict[str, Any]) -> tuple[Dict[str, Any], bool, bool]:
    """Merge verified social-token claims into comment payload.

    Returns (payload, had_invalid_token, had_token).
    """

    token = (
        payload.get("social_token")
        or payload.get("auth_token")
        or request.headers.get("X-Survey-Social-Token")
    )
    token = str(token or "").strip()
    if not token:
        return payload, False, False

    claims = verify_social_comment_token(token)
    if not claims:
        return payload, True, True

    mapping = {
        "provider": "auth_provider",
        "auth_user_id": "auth_user_id",
        "auth_email": "auth_email",
        "auth_first_name": "auth_first_name",
        "auth_last_name": "auth_last_name",
    }
    for claim_key, payload_key in mapping.items():
        incoming = str(payload.get(payload_key) or "").strip()
        claim_value = str(claims.get(claim_key) or "").strip()
        if incoming and claim_value and incoming.lower() != claim_value.lower():
            raise EncuestaError(
                "Los datos sociales no coinciden con el token",
                status_code=400,
                payload={"reason_code": "social_identity_mismatch", "retryable": False},
            )
        if claim_value:
            payload[payload_key] = claim_value

    current_mode = str(payload.get("mode") or payload.get("comment_mode") or "").strip().lower()
    if not current_mode and payload.get("auth_provider"):
        payload["mode"] = "social"
    return payload, False, True


def _resolve_request_id() -> str:
    request_id = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
    )
    if not request_id:
        request_id = uuid.uuid4().hex
    g.request_id = request_id
    return request_id


def _public_error_response(err: EncuestaError, *, fallback_reason: Optional[str] = None):
    payload = err.to_dict() if hasattr(err, "to_dict") else {"error": str(err)}
    status_code = int(getattr(err, "status_code", 500) or 500)
    reason_code = payload.get("reason_code") or fallback_reason
    if status_code == 404 and not reason_code:
        reason_code = "survey_not_found"
    retryable = status_code >= 500
    action_hint_map = {
        "survey_not_published": "view_other_surveys",
        "survey_outside_active_window": "view_other_surveys",
        "rate_limited": "retry_later",
    }
    action_hint = action_hint_map.get(reason_code, "retry")
    if status_code == 404:
        action_hint = "go_home"

    if status_code == 404 and reason_code == "survey_not_found":
        payload["contract_version"] = "public.survey_resolution.v1"
        payload.setdefault("list_endpoint", "/api/public/encuestas")
    else:
        payload.setdefault("contract_version", "encuestas.public_error.v1")
    payload.setdefault("status_code", status_code)
    if reason_code:
        payload["reason_code"] = reason_code
    payload.setdefault("retryable", retryable)
    payload.setdefault("action_hint", action_hint)
    request_id = _resolve_request_id()
    payload.setdefault("request_id", request_id)
    response = jsonify(payload)
    response.headers.setdefault("X-Request-Id", request_id)
    return response, status_code


def _load_public_encuesta_for_request(slug: str, *, preview_user=None):
    tenant_id = _resolve_tenant_from_request()
    return get_public_encuesta(
        slug,
        allow_inactive_for_user=preview_user,
        preferred_tenant_id=tenant_id,
    )


_SHARE_IMAGE_CANDIDATE_KEYS = (
    "share_image_url",
    "imagen_portada_url",
    "portada_url",
    "banner_url",
    "image_url",
    "thumbnail_url",
)


def _resolve_share_image(encuesta: Optional[dict]) -> Optional[str]:
    """Select an appropriate hero/share image for the public landing page."""

    def _clean(value: Optional[str]) -> Optional[str]:
        if not isinstance(value, str):
            return None
        cleaned = value.strip()
        return cleaned or None

    if isinstance(encuesta, dict):
        for key in _SHARE_IMAGE_CANDIDATE_KEYS:
            candidate = _clean(encuesta.get(key))
            if candidate:
                return candidate

        metadata = encuesta.get("metadata")
        if isinstance(metadata, dict):
            for key in _SHARE_IMAGE_CANDIDATE_KEYS:
                candidate = _clean(metadata.get(key))
                if candidate:
                    return candidate

    fallback = _clean(current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"))
    if fallback:
        return fallback

    base = _clean(current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"))
    if not base:
        base = _clean(request.host_url)

    asset_candidate = ENCUESTAS_DEFAULT_SHARE_IMAGE_PATH
    if isinstance(asset_candidate, str) and asset_candidate.startswith(("http://", "https://")):
        return asset_candidate

    if base and asset_candidate:
        return f"{base.rstrip('/')}{asset_candidate}"

    return asset_candidate or None


def _extract_ip() -> str:
    return public_survey_client_ip()


def _iter_allowed_origins() -> Iterator[AllowedOrigin]:
    """Yield allowed CORS origins honoring environment overrides."""

    env_value = os.getenv("CORS_ALLOWED_ORIGINS")
    if env_value is not None:
        stripped = env_value.strip()
        if stripped == "*":
            yield "*"
            return

        seen: set[str] = set()
        for raw in stripped.split(","):
            candidate = raw.strip().rstrip("/")
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            yield candidate

        if seen:
            return

    for origin in DEFAULT_ALLOWED_ORIGINS:
        yield origin


def _resolve_cors_origin(origin: Optional[str]) -> tuple[Optional[str], bool]:
    """Return the value for Access-Control-Allow-Origin and credentials flag."""

    if not origin:
        return None, False

    normalized = origin.rstrip("/")
    for allowed in _iter_allowed_origins():
        if allowed == "*":
            return "*", False
        if isinstance(allowed, str):
            if normalized == allowed.rstrip("/"):
                return origin, True
        elif hasattr(allowed, "match") and allowed.match(origin):
            return origin, True

    return None, False


def _merge_header_values(response, header_name: str, values: list[str]) -> None:
    """Ensure comma-separated headers include the provided values."""

    existing = response.headers.get(header_name, "")
    items = [item.strip() for item in existing.split(",") if item.strip()]

    updated = list(items)
    for value in values:
        if value not in updated:
            updated.append(value)

    if updated:
        response.headers[header_name] = ", ".join(updated)


def _resolve_tenant_from_request(*, explicit_only: bool = False) -> Optional[int]:
    """Infer the tenant/municipio identifier for a public survey listing."""

    arg_candidates = [
        request.args.get("tenant_id", type=int),
        request.args.get("tenant", type=int),
        request.args.get("municipio_id", type=int),
        request.args.get("owner_id", type=int),
        request.args.get("owner", type=int),
    ]

    for candidate in arg_candidates:
        if candidate is not None:
            return candidate

    header_candidates = [
        request.headers.get("X-Tenant-Id"),
        request.headers.get("X-Tenant"),
        request.headers.get("X-Municipio-Id"),
        request.headers.get("X-Owner-Id"),
    ]

    for raw_value in header_candidates:
        if not raw_value:
            continue
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            continue

    slug_candidates = [
        request.args.get("tenant_slug"),
        request.args.get("tenant"),
        request.headers.get("X-Tenant-Slug"),
        request.headers.get("X-Tenant"),
    ]
    for raw_slug in slug_candidates:
        slug = str(raw_slug or "").strip().lower()
        if not slug:
            continue
        tenant = TenantProfile.query.filter_by(slug=slug).first()
        if tenant is not None:
            try:
                return int(tenant.id)
            except (TypeError, ValueError):
                continue

    if explicit_only:
        return None

    token = obtener_token()
    owner_candidate = getattr(g, "_obtener_token_owner", None)
    if owner_candidate is None and token:
        user = user_from_token(token)
        if user is not None:
            owner_candidate = user

    if owner_candidate is not None:
        for attr in ("municipio_id", "empresa_id", "pyme_id", "id"):
            value = getattr(owner_candidate, attr, None)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue

    mapping = current_app.config.get("PUBLIC_ENCUESTAS_DOMAIN_MAP") or {}
    if mapping:
        host_candidates = []
        forwarded = request.headers.get("X-Forwarded-Host")
        if forwarded:
            host_candidates.extend(part.strip() for part in forwarded.split(",") if part.strip())
        host_candidates.extend(
            [
                request.headers.get("Host"),
                request.host,
                request.headers.get("Origin"),
                request.headers.get("Referer"),
            ]
        )

        for candidate in host_candidates:
            normalized = _normalize_host(candidate)
            if not normalized:
                continue
            variants = [normalized]
            if normalized.startswith("www."):
                variants.append(normalized[4:])
            else:
                variants.append(f"www.{normalized}")

            for variant in variants:
                tenant_value = mapping.get(variant)
                if tenant_value is None:
                    continue
                try:
                    return int(tenant_value)
                except (TypeError, ValueError):
                    current_app.logger.warning(
                        "[encuestas] Invalid tenant id '%s' configured for domain '%s'.",
                        tenant_value,
                        variant,
                    )

    default_tenant = current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID")
    if default_tenant not in (None, ""):
        try:
            return int(default_tenant)
        except (TypeError, ValueError):
            current_app.logger.warning(
                "[encuestas] Invalid PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID value '%s'.",
                default_tenant,
            )

    return None


def _create_public_blueprint(name: str, url_prefix: str) -> Blueprint:
    bp = Blueprint(name, __name__, url_prefix=url_prefix)

    @bp.before_request
    def _check_feature():  # pragma: no cover - simple guard
        guard = _feature_guard()
        if guard:
            return guard
        return None

    @bp.after_request
    def _apply_cors(response):
        allowed_origin, allow_credentials = _resolve_cors_origin(
            request.headers.get("Origin")
        )

        if allowed_origin:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin

            if allowed_origin != "*":
                _merge_header_values(response, "Vary", ["Origin"])
                if allow_credentials:
                    response.headers["Access-Control-Allow-Credentials"] = "true"
            else:
                response.headers.pop("Access-Control-Allow-Credentials", None)

            _merge_header_values(
                response,
                "Access-Control-Allow-Headers",
                [
                    "Authorization",
                    "Content-Type",
                    "Origin",
                    "Accept",
                    "X-Entity-Token",
                    "X-Chat-Session-Id",
                    "X-Anon-Id",
                    "Anon-Id",
                    "X-Tenant-Slug",
                    "X-Tenant",
                    "Idempotency-Key",
                    "X-Turnstile-Token",
                ],
            )

            _merge_header_values(
                response,
                "Access-Control-Allow-Methods",
                ["GET", "POST", "OPTIONS"],
            )
        else:
            response.headers.pop("Access-Control-Allow-Credentials", None)

        return response

    @bp.route("", methods=["GET", "OPTIONS"])
    @bp.route("/v1", methods=["GET", "OPTIONS"])
    def listar_publicas():
        if request.method == "OPTIONS":
            return "", 204

        demo_contract = _demo_survey_list_contract_from_request()
        if demo_contract:
            payload = demo_contract.get("items") or []
            if request.path.rstrip("/").endswith("/v1"):
                request_id = _resolve_request_id()
                response = jsonify(
                    {
                        "contract_version": "encuestas.public_list.v1",
                        "demo_mode": True,
                        "items": payload,
                        "count": len(payload),
                        "pagination": {
                            "page": demo_contract.get("page"),
                            "page_size": demo_contract.get("page_size"),
                            "has_more": demo_contract.get("has_more"),
                            "next_action_id": demo_contract.get("next_action_id"),
                            "previous_action_id": demo_contract.get("previous_action_id"),
                        },
                        "seed_policy": demo_contract.get("seed_policy"),
                        "request_id": request_id,
                    }
                )
                response.headers.setdefault("X-Request-Id", request_id)
                return response
            return jsonify(payload)

        tenant_id = _resolve_tenant_from_request()
        if tenant_id is None:
            tenant_id = current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID") or 4

        limit = request.args.get("limit", default=5, type=int) or 5
        if limit < 0:
            limit = 5

        try:
            encuestas = list_public_encuestas_for_tenant(tenant_id, limit=limit)
        except Exception:  # pragma: no cover - defensive logging
            current_app.logger.exception(
                "No se pudieron listar las encuestas públicas para el tenant %s",
                tenant_id,
            )
            wrapped = EncuestaError(
                "No se pudieron obtener las encuestas.",
                status_code=500,
                payload={"reason_code": "internal_error"},
            )
            return _public_error_response(wrapped)

        base_url = _public_target_base_url()
        payload = []
        for encuesta, slug in encuestas:
            data = _attach_comment_social_config(
                _serialize_public_encuesta_summary(encuesta, slug, base_url)
            )
            payload.append(data)

        if request.path.rstrip("/").endswith("/v1"):
            request_id = _resolve_request_id()
            response = jsonify(
                {
                    "contract_version": "encuestas.public_list.v1",
                    "items": payload,
                    "count": len(payload),
                    "request_id": request_id,
                }
            )
            response.headers.setdefault("X-Request-Id", request_id)
            return response

        return jsonify(payload)

    @bp.route("/<slug>", methods=["GET"])
    @bp.route("/v1/<slug>", methods=["GET"])
    def obtener_encuesta(slug: str):
        if is_demo_survey_slug(slug):
            payload = build_demo_public_survey_payload(
                slug,
                public_base_url=_public_target_base_url(),
            )
            if payload:
                payload = _attach_comment_social_config(payload)
                request_id = _resolve_request_id()
                payload.setdefault("request_id", request_id)
                response = jsonify(payload)
                response.headers.setdefault("X-Request-Id", request_id)
                return response

        preview_user = _resolve_preview_user()
        try:
            encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
        except EncuestaError as err:
            return _public_error_response(err)
        payload = _attach_comment_social_config(serialize_public_encuesta(encuesta, slug_publico=slug))
        payload.setdefault("contract_version", "encuestas.public.v1")
        request_id = _resolve_request_id()
        payload.setdefault("request_id", request_id)
        response = jsonify(payload)
        response.headers.setdefault("X-Request-Id", request_id)
        return response

    def _response_ack(respuesta, *, request_id: str):
        from services.survey_governance import response_governance_contract

        response_payload = {
            "contract_version": ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION,
            "ok": True,
            "success": True,
            "persisted": True,
            "replayed": bool(getattr(respuesta, "submission_replayed", False)),
            "respuesta_id": respuesta.id,
            "response_id": respuesta.id,
            "instrument_revision": getattr(respuesta, "instrument_revision", None),
            "request_id": request_id,
            "governance": response_governance_contract(respuesta),
        }
        receipt_contract = survey_response_receipt_contract(respuesta)
        if receipt_contract is not None:
            response_payload["idempotency"] = receipt_contract
        response = jsonify(response_payload)
        response.headers.setdefault("X-Request-Id", request_id)
        return response, 200 if response_payload["replayed"] else 201

    def _handle_responder(slug: str):
        ip = _extract_ip()
        tenant_id = _resolve_tenant_from_request()
        explicit_tenant_id = _resolve_tenant_from_request(explicit_only=True)
        payload = _extract_request_payload()
        request_id = _resolve_request_id()
        try:
            submission_id = resolve_survey_submission_id(
                payload,
                header_value=request.headers.get("Idempotency-Key"),
                # Demo acknowledgements are synthetic and non-durable. All
                # canonical writes require a stable caller-owned key.
                required=not is_demo_survey_slug(slug),
            )
        except EncuestaError as err:
            return _public_error_response(err)
        request_ctx = {
            "ip": ip,
            "user_agent": request.headers.get("User-Agent"),
            "anon_id": request.cookies.get("Anon-Id")
            or request.headers.get("X-Anon-Id"),
            "canal": request.args.get("canal"),
        }
        authenticated_user = None
        authenticated_user_resolved = False
        if submission_id is not None and not is_demo_survey_slug(slug):
            try:
                authenticated_user = resolve_optional_survey_bearer_user(
                    request.headers.get("Authorization"),
                    contract_version=ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION,
                )
                authenticated_user_resolved = True
                replay = find_survey_response_replay(
                    slug,
                    payload,
                    request_ctx,
                    submission_id=submission_id,
                    preferred_tenant_id=tenant_id,
                    authenticated_user=authenticated_user,
                )
            except EncuestaError as err:
                # The replay lookup is an optimization that lets committed
                # retries bypass one-shot controls.  Survey resolution remains
                # authoritative inside ``save_respuesta``; a miss must fall
                # through so every public alias reuses that single handler.
                if err.status_code != 404:
                    return _public_error_response(err)
                replay = None
            if replay is not None:
                replay_scope = enforce_public_survey_replay_scope(
                    replay,
                    preferred_tenant_id=explicit_tenant_id,
                )
                if replay_scope is not None:
                    error_payload = replay_scope.error_payload()
                    error_payload["request_id"] = request_id
                    response = jsonify(error_payload)
                    response.headers.setdefault("X-Request-Id", request_id)
                    return response, replay_scope.status_code or 404
                return _response_ack(replay, request_id=request_id)

        intake_decision = enforce_public_survey_intake(
            slug,
            payload,
            preferred_tenant_id=explicit_tenant_id,
            request_id=request_id,
            synthetic=is_demo_survey_slug(slug),
        )
        if not intake_decision.allowed:
            error_payload = intake_decision.error_payload()
            error_payload["request_id"] = request_id
            response = jsonify(error_payload)
            response.headers.setdefault("X-Request-Id", request_id)
            attach_public_survey_rate_limit_headers(
                response,
                intake_decision.rate_limit,
            )
            return response, intake_decision.status_code or 503

        demo_ack = build_demo_survey_response_ack(slug, payload)
        if demo_ack:
            demo_ack["request_id"] = request_id
            response = jsonify(demo_ack)
            response.headers.setdefault("X-Request-Id", request_id)
            attach_public_survey_rate_limit_headers(
                response,
                intake_decision.rate_limit,
            )
            return response, 201

        try:
            if not authenticated_user_resolved:
                authenticated_user = resolve_optional_survey_bearer_user(
                    request.headers.get("Authorization"),
                    contract_version=ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION,
                )
            respuesta = save_respuesta(
                slug,
                payload,
                request_ctx,
                preferred_tenant_id=tenant_id,
                authenticated_user=authenticated_user,
                submission_id=submission_id,
            )
        except EncuestaError as err:
            reason_code = str((err.payload or {}).get("reason_code") or "").strip()
            if err.status_code == 409 and reason_code == "survey_response_duplicate":
                return (
                    jsonify(
                        {
                            "contract_version": ENCUESTAS_PUBLIC_RESPONSE_CONTRACT_VERSION,
                            "ok": True,
                            "duplicate": True,
                            "reason_code": reason_code,
                            "retryable": False,
                            "message": "Ya registramos tu participación",
                            "suggested_admin_endpoint_template": "/admin/encuestas/{encuesta_id}/seed-demo/bulk",
                        }
                    ),
                    200,
                )
            return _public_error_response(err)
        response, status_code = _response_ack(respuesta, request_id=request_id)
        attach_public_survey_rate_limit_headers(
            response,
            intake_decision.rate_limit,
        )
        return response, status_code

    @bp.route("/<slug>/responder", methods=["POST", "OPTIONS"])
    @bp.route("/v1/<slug>/responder", methods=["POST", "OPTIONS"])
    def responder(slug: str):
        if request.method == "OPTIONS":
            return "", 204
        return _handle_responder(slug)

    @bp.route("/<slug>/respuestas", methods=["POST", "OPTIONS"])
    @bp.route("/v1/<slug>/respuestas", methods=["POST", "OPTIONS"])
    def responder_alias(slug: str):
        if request.method == "OPTIONS":
            return "", 204
        return _handle_responder(slug)

    @bp.route("/<slug>/live-results", methods=["GET", "OPTIONS"])
    @bp.route("/v1/<slug>/live-results", methods=["GET", "OPTIONS"])
    def live_results(slug: str):
        if request.method == "OPTIONS":
            return "", 204

        demo_results = build_demo_live_results_payload(
            slug,
            public_base_url=_public_target_base_url(),
        )
        if demo_results:
            request_id = _resolve_request_id()
            demo_results.setdefault("request_id", request_id)
            response = jsonify(demo_results)
            response.headers.setdefault("X-Request-Id", request_id)
            return response

        include_heatmap = request.args.get("include_heatmap", "1").strip().lower() not in {"0", "false", "no", "off"}
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

        try:
            tenant_id = _resolve_tenant_from_request()
            results = calculate_live_results(
                slug,
                preferred_tenant_id=tenant_id,
                include_heatmap=include_heatmap,
                max_points=max(100, min(max_points, 5000)),
                max_cells=max(50, min(max_cells, 1000)),
                momentum_window_minutes=momentum_window_minutes,
                filtros=filtros,
            )
            request_id = _resolve_request_id()
            results.setdefault("request_id", request_id)
            response = jsonify(results)
            response.headers.setdefault("X-Request-Id", request_id)
            return response
        except EncuestaError as err:
            return _public_error_response(err)
        except Exception as e:
            current_app.logger.error(f"Error fetching live results for {slug}: {e}")
            wrapped = EncuestaError(
                "Error interno",
                status_code=500,
                payload={"reason_code": "internal_error"},
            )
            return _public_error_response(wrapped)

    @bp.route("/<slug>/comentarios", methods=["GET", "POST", "OPTIONS"])
    @bp.route("/v1/<slug>/comentarios", methods=["GET", "POST", "OPTIONS"])
    def comentarios(slug: str):
        if request.method == "OPTIONS":
            return "", 204

        if is_demo_survey_slug(slug):
            if request.method == "POST":
                return jsonify(
                    {
                        "ok": True,
                        "demo_mode": True,
                        "comment": None,
                        "message": "Comentario demo recibido sin persistir datos reales.",
                    }
                ), 201
            return jsonify([])

        preview_user = _resolve_preview_user()
        try:
            encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
        except EncuestaError as err:
            return _public_error_response(err)

        if request.method == "POST":
            # For comments, we might want to know who the user is
            token = obtener_token()
            user = user_from_token(token) if token else None

            payload = _extract_request_payload()
            if not isinstance(payload, dict):
                payload = {}
            try:
                payload, invalid_social_token, has_social_token = _enrich_comment_payload_with_social_token(payload)
                requires_social_token = _coerce_bool(
                    current_app.config.get("SURVEY_SOCIAL_COMMENT_REQUIRE_TOKEN"),
                    default=False,
                )
                comment_mode = str(payload.get("mode") or payload.get("comment_mode") or "").strip().lower()
                is_social_comment = comment_mode == "social"
                if invalid_social_token and (is_social_comment or requires_social_token):
                    raise EncuestaError(
                        "Token social inválido o expirado",
                        status_code=400,
                        payload={"reason_code": "invalid_social_token", "retryable": False},
                    )
                if requires_social_token and is_social_comment and not has_social_token:
                    raise EncuestaError(
                        "Se requiere token social válido para comentar",
                        status_code=400,
                        payload={"reason_code": "social_token_required", "retryable": False},
                    )

                comentario = create_comentario(encuesta.id, payload, user)
                return jsonify({
                    "ok": True,
                    "comentario": serialize_public_comment(comentario),
                }), 201
            except EncuestaError as err:
                return _public_error_response(err)

        # GET
        limit = request.args.get("limit", default=50, type=int)
        offset = request.args.get("offset", default=0, type=int)
        items = list_comentarios(encuesta.id, limit=limit, offset=offset)
        return jsonify(items)

    @bp.route("/<slug>/comentarios/<int:comentario_id>/reportar", methods=["POST", "OPTIONS"])
    @bp.route("/v1/<slug>/comentarios/<int:comentario_id>/reportar", methods=["POST", "OPTIONS"])
    def reportar_comment(slug: str, comentario_id: int):
        if request.method == "OPTIONS":
            return "", 204

        try:
            # We fetch encuesta just to ensure the slug is valid, though report doesn't strictly depend on it in service
            _load_public_encuesta_for_request(slug)
            reportar_comentario(comentario_id)
            return jsonify({"ok": True}), 200
        except EncuestaError as err:
            return _public_error_response(err)

    @bp.route("/<slug>/qr")
    @bp.route("/v1/<slug>/qr")
    def qr(slug: str):
        preview_user = _resolve_preview_user()

        if not is_demo_survey_slug(slug):
            try:
                _load_public_encuesta_for_request(slug, preview_user=preview_user)
            except EncuestaError as err:
                return _public_error_response(err)

        size = request.args.get("size", default=320, type=int)
        base_url = _public_target_base_url()
        url = f"{base_url}/e/{slug}"
        try:
            png = build_qr_png(url, size=size)
        except ValueError as exc:
            wrapped = EncuestaError(
                str(exc),
                status_code=400,
                payload={"reason_code": "invalid_qr_size", "retryable": False},
            )
            return _public_error_response(wrapped)
        return send_file(
            io.BytesIO(png),
            mimetype="image/png",
            download_name=f"encuesta-{slug}.png",
        )

    return bp


encuestas_public_bp = _create_public_blueprint(
    "encuestas_public_bp", "/api/public/encuestas"
)
encuestas_public_legacy_bp = _create_public_blueprint(
    "encuestas_public_legacy_bp", "/public/encuestas"
)


encuestas_public_share_bp = Blueprint("encuestas_public_share_bp", __name__)


@encuestas_public_share_bp.before_request
def _share_check_feature():
    guard = _feature_guard()
    if guard:
        return guard
    return None


@encuestas_public_share_bp.route("/e/<slug>", methods=["GET"])
def share_redirect(slug: str):
    canonical = current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    if canonical:
        canonical = canonical.rstrip("/")
        target = f"{canonical}/e/{slug}"
        request_base = request.host_url.rstrip("/")
        if request_base != canonical:
            return redirect(target, code=302)

    accept = request.accept_mimetypes
    wants_json = accept.best == "application/json" and accept[accept.best] >= accept["text/html"]

    if is_demo_survey_slug(slug):
        data = build_demo_public_survey_payload(
            slug,
            public_base_url=_public_target_base_url(),
        )
        if data:
            data = _attach_comment_social_config(data)
            base_url = _public_target_base_url()
            share_url = f"{base_url}/e/{slug}"
            api_base_url = _public_api_base_url()
            qr_url = f"{api_base_url}/api/public/encuestas/{slug}/qr"
            widget_url = f"{share_url}?canal=widget_chat"
            titulo = data.get("titulo") or "Encuesta demo"
            whatsapp_message = f"Participa en '{titulo}' ingresando a {share_url}"
            whatsapp_url = f"https://wa.me/?text={quote_plus(whatsapp_message)}"
            share_image_url = _resolve_share_image(data)
            if wants_json:
                return jsonify(data)
            return render_template(
                "encuestas/share.html",
                encuesta=data,
                share_url=share_url,
                qr_url=qr_url,
                widget_url=widget_url,
                whatsapp_url=whatsapp_url,
                whatsapp_message=whatsapp_message,
                share_image_url=share_image_url,
            )

    preview_user = _resolve_preview_user()
    try:
        encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
    except EncuestaError as err:
        if wants_json:
            return _public_error_response(err)
        return (
            render_template(
                "encuestas/share.html",
                encuesta=None,
                error=err.to_dict(),
                status_code=err.status_code,
                share_url=None,
                qr_url=None,
                widget_url=None,
                whatsapp_url=None,
                whatsapp_message=None,
                share_image_url=_resolve_share_image(None),
            ),
            err.status_code,
        )

    data = _attach_comment_social_config(serialize_public_encuesta(encuesta, slug_publico=slug))
    base_url = _public_target_base_url()
    share_url = f"{base_url}/e/{slug}"
    api_base_url = _public_api_base_url()
    qr_url = f"{api_base_url}/api/public/encuestas/{slug}/qr"
    widget_url = f"{share_url}?canal=widget_chat"
    titulo = data.get("titulo") or "Encuesta ciudadana"
    whatsapp_message = f"Participá en '{titulo}' ingresando a {share_url}"
    whatsapp_url = f"https://wa.me/?text={quote_plus(whatsapp_message)}"
    share_image_url = _resolve_share_image(data)

    if wants_json:
        return jsonify(data)

    return render_template(
        "encuestas/share.html",
        encuesta=data,
        share_url=share_url,
        qr_url=qr_url,
        widget_url=widget_url,
        whatsapp_url=whatsapp_url,
        whatsapp_message=whatsapp_message,
        share_image_url=share_image_url,
    )
