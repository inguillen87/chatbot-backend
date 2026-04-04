"""Public endpoints for answering and listing active surveys."""
from __future__ import annotations

import io
import json
import os
import time
from collections import defaultdict, deque
import re
from threading import Lock
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
    get_public_encuesta,
    list_public_encuestas_for_tenant,
    save_respuesta,
    serialize_public_encuesta,
    create_comentario,
    list_comentarios,
    reportar_comentario,
)
from services.encuestas_analytics_service import calculate_live_results
from utils.auth_helpers import obtener_token, user_from_token

_DEFAULT_RATE_LIMIT = 150
_DEFAULT_RATE_PERIOD = 60
_rate_buckets: defaultdict[str, deque] = defaultdict(deque)
_rate_lock = Lock()

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
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def _resolve_rate_settings() -> tuple[int, int]:
    limit = current_app.config.get("PUBLIC_ENCUESTAS_RATE_LIMIT")
    period = current_app.config.get("PUBLIC_ENCUESTAS_RATE_PERIOD")

    if limit is None:
        limit = os.getenv("PUBLIC_ENCUESTAS_RATE_LIMIT")
    if period is None:
        period = os.getenv("PUBLIC_ENCUESTAS_RATE_PERIOD")

    resolved_limit = _coerce_positive_int(limit, _DEFAULT_RATE_LIMIT)
    resolved_period = _coerce_positive_int(period, _DEFAULT_RATE_PERIOD)
    return resolved_limit, resolved_period


def _rate_limit(ip: str) -> bool:
    limit, period = _resolve_rate_settings()
    if limit <= 0 or period <= 0:
        return True

    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > period:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        return True


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


def _resolve_tenant_from_request() -> Optional[int]:
    """Infer the tenant/municipio identifier for a public survey listing."""

    arg_candidates = [
        request.args.get("tenant_id", type=int),
        request.args.get("municipio_id", type=int),
        request.args.get("owner_id", type=int),
        request.args.get("owner", type=int),
    ]

    for candidate in arg_candidates:
        if candidate is not None:
            return candidate

    header_candidates = [
        request.headers.get("X-Tenant-Id"),
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
    def listar_publicas():
        if request.method == "OPTIONS":
            return "", 204

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
            return (
                jsonify({"error": "No se pudieron obtener las encuestas."}),
                500,
            )

        base_url = _public_target_base_url()
        payload = []
        for encuesta, slug in encuestas:
            data = serialize_public_encuesta(encuesta, slug_publico=slug)
            data["url_publica"] = f"{base_url}/e/{slug}"
            payload.append(data)

        return jsonify(payload)

    @bp.route("/<slug>", methods=["GET"])
    def obtener_encuesta(slug: str):
        preview_user = _resolve_preview_user()
        try:
            encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_public_encuesta(encuesta, slug_publico=slug))

    def _handle_responder(slug: str):
        ip = _extract_ip()
        tenant_id = _resolve_tenant_from_request()
        if not _rate_limit(ip):
            return (
                jsonify({
                    "error": "Demasiadas respuestas desde esta IP. Intenta más tarde.",
                }),
                429,
            )

        request_ctx = {
            "ip": ip,
            "user_agent": request.headers.get("User-Agent"),
            "anon_id": request.cookies.get("Anon-Id")
            or request.headers.get("X-Anon-Id"),
            "canal": request.args.get("canal"),
        }
        payload = _extract_request_payload()
        try:
            respuesta = save_respuesta(
                slug,
                payload,
                request_ctx,
                preferred_tenant_id=tenant_id,
            )
        except EncuestaError as err:
            if err.status_code == 409:
                return (
                    jsonify(
                        {
                            "ok": True,
                            "duplicate": True,
                            "message": "Ya registramos tu participación",
                            "suggested_admin_endpoint_template": "/admin/encuestas/{encuesta_id}/seed-demo/bulk",
                        }
                    ),
                    200,
                )
            return jsonify(err.to_dict()), err.status_code
        return jsonify({"ok": True, "respuesta_id": respuesta.id}), 201

    @bp.route("/<slug>/responder", methods=["POST", "OPTIONS"])
    def responder(slug: str):
        if request.method == "OPTIONS":
            return "", 204
        return _handle_responder(slug)

    @bp.route("/<slug>/respuestas", methods=["POST", "OPTIONS"])
    def responder_alias(slug: str):
        if request.method == "OPTIONS":
            return "", 204
        return _handle_responder(slug)

    @bp.route("/<slug>/live-results", methods=["GET", "OPTIONS"])
    def live_results(slug: str):
        if request.method == "OPTIONS":
            return "", 204

        include_heatmap = request.args.get("include_heatmap", "1").strip().lower() not in {"0", "false", "no", "off"}
        max_points = request.args.get("max_points", default=2000, type=int) or 2000
        max_cells = request.args.get("max_cells", default=200, type=int) or 200
        window_minutes = request.args.get("window_minutes", default=10, type=int) or 10

        try:
            # Reusing existing service/analytics logic
            results = calculate_live_results(
                slug,
                include_heatmap=include_heatmap,
                max_points=max(100, min(max_points, 5000)),
                max_cells=max(50, min(max_cells, 1000)),
                momentum_window_minutes=window_minutes,
            )
            return jsonify(results)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        except Exception as e:
            current_app.logger.error(f"Error fetching live results for {slug}: {e}")
            return jsonify({"error": "Error interno"}), 500

    @bp.route("/<slug>/comentarios", methods=["GET", "POST", "OPTIONS"])
    def comentarios(slug: str):
        if request.method == "OPTIONS":
            return "", 204

        preview_user = _resolve_preview_user()
        try:
            encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        if request.method == "POST":
            # For comments, we might want to know who the user is
            token = obtener_token()
            user = user_from_token(token) if token else None

            payload = _extract_request_payload()
            try:
                comentario = create_comentario(encuesta.id, payload, user)
                return jsonify({
                    "ok": True,
                    "comentario": {
                        "id": comentario.id,
                        "texto": comentario.texto,
                        "nombre_autor": comentario.nombre_autor,
                        "fecha": comentario.created_at.isoformat()
                    }
                }), 201
            except EncuestaError as err:
                return jsonify(err.to_dict()), err.status_code

        # GET
        limit = request.args.get("limit", default=50, type=int)
        offset = request.args.get("offset", default=0, type=int)
        items = list_comentarios(encuesta.id, limit=limit, offset=offset)
        return jsonify(items)

    @bp.route("/<slug>/comentarios/<int:comentario_id>/reportar", methods=["POST", "OPTIONS"])
    def reportar_comment(slug: str, comentario_id: int):
        if request.method == "OPTIONS":
            return "", 204

        try:
            # We fetch encuesta just to ensure the slug is valid, though report doesn't strictly depend on it in service
            _load_public_encuesta_for_request(slug)
            reportar_comentario(comentario_id)
            return jsonify({"ok": True}), 200
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

    @bp.route("/<slug>/qr")
    def qr(slug: str):
        preview_user = _resolve_preview_user()

        try:
            encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        size = request.args.get("size", default=320, type=int)
        base_url = _public_target_base_url()
        url = f"{base_url}/e/{slug}"
        try:
            png = build_qr_png(url, size=size)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
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

    preview_user = _resolve_preview_user()
    try:
        encuesta = _load_public_encuesta_for_request(slug, preview_user=preview_user)
    except EncuestaError as err:
        if wants_json:
            return jsonify(err.to_dict()), err.status_code
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

    data = serialize_public_encuesta(encuesta, slug_publico=slug)
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
