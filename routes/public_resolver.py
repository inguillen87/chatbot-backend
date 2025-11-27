from flask import Blueprint, jsonify, request, g, current_app
from flask_cors import cross_origin

from models import TenantProfile
from services.tenant_resolver import (
    RESERVED_TENANT_SLUGS,
    TenantResolutionError,
    inject_anon_cookie,
    resolve_tenant_and_user,
    resolve_tenant_only,
)

from utils.auth_helpers import _is_jwt_token

public_resolver_bp = Blueprint("public_resolver_bp", __name__, url_prefix="/api/public")


def _extract_widget_token() -> str | None:
    """Read the widget/entity token from query params, headers or cookies."""

    widget_cookie_name = (
        current_app.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
        if current_app
        else "widget_token"
    )

    candidates = [
        request.args.get("widget_token"),
        request.args.get("entityToken"),
        request.args.get("owner_token") or request.args.get("ownerToken"),
        request.headers.get("X-Widget-Token"),
        request.headers.get("X-Entity-Token"),
        request.headers.get("X-Owner-Token"),
        request.cookies.get(widget_cookie_name),
        request.cookies.get("owner_token"),
    ]

    auth_header = request.headers.get("Authorization") or ""
    if auth_header.lower().startswith("bearer "):
        candidate = auth_header.split(None, 1)[1]
        if candidate and not _is_jwt_token(candidate):
            candidates.append(candidate)

    for token in candidates:
        if token:
            return token
    return None


@public_resolver_bp.route("/resolve-tenant", methods=["POST"])
def resolve_tenant_endpoint():
    payload = request.get_json(force=True, silent=True) or {}
    whatsapp_destination_number = payload.get("whatsapp_destination_number")
    widget_token = payload.get("widget_token")
    tenant_slug = payload.get("tenant_slug") or request.args.get("tenant")

    try:
        tenant, user, created_anon = resolve_tenant_and_user(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
            current_user=getattr(g, "user", None),
        )
    except TenantResolutionError as exc:
        return jsonify({"error": str(exc)}), 404

    tenant_info = tenant.to_public_dict()
    tenant_info.setdefault("config", tenant.configuracion or {})

    response = jsonify(
        {
            "tenant": tenant_info,
            "anonId": user.anon_id,
            "userId": user.id,
            "created": created_anon,
        }
    )
    inject_anon_cookie(response, user.anon_id)
    return response


@public_resolver_bp.route("/tenant-profile", methods=["GET", "OPTIONS"])
@cross_origin(origins="*")
def tenant_profile():
    """Devuelve datos públicos del tenant sin requerir autenticación.

    Se puede resolver por slug (``tenant``/``slug``), token de widget o número
    de WhatsApp. Siempre responde JSON para evitar páginas HTML de error que
    rompan el widget.
    """

    resolved_from_fallback = False
    resolution_error = None

    tenant_slug_original = request.args.get("tenant") or request.args.get("slug")
    tenant_slug = tenant_slug_original.strip() if tenant_slug_original else None

    if tenant_slug and tenant_slug.lower() in RESERVED_TENANT_SLUGS:
        resolved_from_fallback = True
        resolution_error = (
            f"Tenant slug '{tenant_slug_original}' is reserved; using default tenant"
        )
        tenant_slug = None
    widget_token = _extract_widget_token()
    whatsapp_destination_number = request.args.get("whatsapp_destination_number")

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    try:
        tenant = resolve_tenant_only(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
            require_explicit_slug=bool(tenant_slug),
        )
    except TenantResolutionError as exc:
        resolution_error = resolution_error or str(exc)
        if resolved_from_fallback:
            tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()
            if not tenant:
                placeholder = {
                    "id": None,
                    "slug": "default",
                    "nombre": None,
                    "tipo": None,
                    "logo_url": None,
                    "dominio": request.host,
                    "tema": {},
                    "config": {},
                }
                payload = {
                    "tenant": placeholder,
                    "warning": {
                        "message": resolution_error,
                        "fallback": "placeholder",
                    },
                }
                return jsonify(payload)
            resolved_from_fallback = True
        else:
            return jsonify({"error": resolution_error}), 404

    if (
        tenant_slug_original
        and tenant_slug
        and tenant_slug.lower() != tenant.slug.lower()
        and not resolved_from_fallback
    ):
        resolved_from_fallback = True
        resolution_error = (
            f"Tenant '{tenant_slug_original}' not found; using '{tenant.slug}' instead"
        )

    tenant_info = tenant.to_public_dict()
    tenant_info.setdefault("config", tenant.configuracion or {})

    payload = {"tenant": tenant_info}
    if resolved_from_fallback and resolution_error:
        payload["warning"] = {
            "message": resolution_error,
            "fallback": "default_tenant",
        }

    return jsonify(payload)

