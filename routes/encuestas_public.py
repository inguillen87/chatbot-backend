"""Public endpoints for answering and listing active surveys."""
from __future__ import annotations

import io
import time
from collections import defaultdict, deque
import re
from threading import Lock
from typing import Optional

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from urllib.parse import quote_plus

from config.feature_flags import FEATURE_ENCUESTAS
from services.encuestas_qr_service import build_qr_png
from services.encuestas_service import (
    EncuestaError,
    get_public_encuesta,
    list_public_encuestas_for_tenant,
    save_respuesta,
    serialize_public_encuesta,
)
from utils.auth_helpers import obtener_token, user_from_token

_RATE_LIMIT = 30
_RATE_PERIOD = 60
_rate_buckets: defaultdict[str, deque] = defaultdict(deque)
_rate_lock = Lock()


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


def _public_base_url() -> str:
    configured = current_app.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return request.host_url.rstrip("/")


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

    fallback = current_app.config.get("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL")
    return _clean(fallback)


def _extract_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"


def _rate_limit(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[ip]
        while bucket and now - bucket[0] > _RATE_PERIOD:
            bucket.popleft()
        if len(bucket) >= _RATE_LIMIT:
            return False
        bucket.append(now)
        return True


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

        base_url = _public_base_url()
        payload = []
        for encuesta, slug in encuestas:
            data = serialize_public_encuesta(encuesta, slug_publico=slug)
            data["url_publica"] = f"{base_url}/e/{slug}"
            payload.append(data)

        return jsonify(payload)

    @bp.route("/<slug>", methods=["GET"])
    def obtener_encuesta(slug: str):
        try:
            encuesta = get_public_encuesta(slug)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code
        return jsonify(serialize_public_encuesta(encuesta, slug_publico=slug))

    def _handle_responder(slug: str):
        ip = _extract_ip()
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
        try:
            respuesta = save_respuesta(slug, request.get_json(force=True), request_ctx)
        except EncuestaError as err:
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

    @bp.route("/<slug>/qr")
    def qr(slug: str):
        preview_user = None
        token = obtener_token()
        if token:
            preview_user = user_from_token(token)

        try:
            encuesta = get_public_encuesta(slug, allow_inactive_for_user=preview_user)
        except EncuestaError as err:
            return jsonify(err.to_dict()), err.status_code

        size = request.args.get("size", default=320, type=int)
        base_url = _public_base_url()
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

    try:
        encuesta = get_public_encuesta(slug)
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
    base_url = _public_base_url()
    share_url = f"{base_url}/e/{slug}"
    qr_url = url_for("encuestas_public_bp.qr", slug=slug, _external=True)
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

