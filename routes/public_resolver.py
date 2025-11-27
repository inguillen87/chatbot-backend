from flask import Blueprint, jsonify, request, g
from flask_cors import cross_origin

from models import TenantProfile
from services.tenant_resolver import (
    RESERVED_TENANT_SLUGS,
    TenantResolutionError,
    inject_anon_cookie,
    resolve_tenant_and_user,
    resolve_tenant_only,
)

public_resolver_bp = Blueprint("public_resolver_bp", __name__, url_prefix="/api/public")


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
@cross_origin(origins="*", supports_credentials=True, automatic_options=False)
def tenant_profile():
    """Devuelve datos públicos del tenant sin requerir autenticación.

    Se puede resolver por slug (``tenant``/``slug``), token de widget o número
    de WhatsApp. Siempre responde JSON para evitar páginas HTML de error que
    rompan el widget.
    """

    resolved_from_fallback = False
    resolution_error = None

    tenant_slug = request.args.get("tenant") or request.args.get("slug")
    tenant_slug_original = tenant_slug
    if tenant_slug and tenant_slug.strip().lower() in RESERVED_TENANT_SLUGS:
        tenant_slug = None
        resolved_from_fallback = True
        resolution_error = (
            f"Tenant slug '{tenant_slug_original}' is reserved; using default tenant"
        )

    if tenant_slug is not None:
        tenant_slug = tenant_slug.strip() or None
    widget_token = (
        request.args.get("widget_token")
        or request.headers.get("X-Widget-Token")
        or request.args.get("entityToken")
        or request.headers.get("X-Entity-Token")
    )
    whatsapp_destination_number = request.args.get("whatsapp_destination_number")

    if request.method == "OPTIONS":
        return jsonify({"ok": True})

    try:
        tenant = resolve_tenant_only(
            whatsapp_destination_number=whatsapp_destination_number,
            widget_token=widget_token,
            tenant_slug=tenant_slug,
        )
    except TenantResolutionError as exc:
        resolution_error = str(exc)
        tenant = TenantProfile.query.order_by(TenantProfile.id.asc()).first()
        if tenant:
            resolved_from_fallback = True
        else:
            placeholder = {
                "id": None,
                "slug": tenant_slug or "default",
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

