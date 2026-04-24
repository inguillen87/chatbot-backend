import time

from services.config_loader import cargar_configuracion_municipio


def _resolve_municipio_config(
    *,
    owner_user: object | None = None,
    tenant_profile: object | None = None,
    municipio_config: dict | None = None,
) -> dict:
    if isinstance(municipio_config, dict):
        return municipio_config

    if tenant_profile and isinstance(getattr(tenant_profile, "configuracion", None), dict):
        return tenant_profile.configuracion

    municipio_id = None
    if tenant_profile and getattr(tenant_profile, "municipio_id", None):
        municipio_id = tenant_profile.municipio_id
    elif owner_user and getattr(owner_user, "municipio_id", None):
        municipio_id = owner_user.municipio_id

    if municipio_id:
        config = cargar_configuracion_municipio(str(municipio_id), "config.json")
        if isinstance(config, dict):
            return config

    return {}


def get_active_promo(
    *,
    owner_user: object | None = None,
    tenant_profile: object | None = None,
    municipio_config: dict | None = None,
) -> dict:
    """Return the currently active promotional configuration."""

    config = _resolve_municipio_config(
        owner_user=owner_user,
        tenant_profile=tenant_profile,
        municipio_config=municipio_config,
    )
    promo = None
    if isinstance(config.get("promo_section"), dict):
        promo = config.get("promo_section")
    elif isinstance(config.get("promo"), dict):
        promo = config.get("promo")

    return promo.copy() if promo else {}


from flask import g

def build_ticket_promo_section(
    ticket_number: str | None = None,
    neighbor_name: str | None = None,
    owner_user: object | None = None,
    tenant_profile: object | None = None,
    municipio_config: dict | None = None,
) -> dict | None:
    """Build a promo snippet to append to the ticket confirmation message."""

    # Prevent leakage: only show promo for municipal tenants.
    is_municipio = False

    # Check explicit arguments first
    if tenant_profile:
        # Strict check for municipal tenants
        if getattr(tenant_profile, "tipo", "") == "municipio" or getattr(tenant_profile, "type", "") == "government":
            is_municipio = True
        elif getattr(tenant_profile, "slug", "") in ["municipio", "junin", "municipalidad-de-junin"]:
            is_municipio = True
        else:
            # If a tenant profile was provided but it's NOT a municipality,
            # explicitly STOP here. Do not check owner_user or fall back to globals.
            return None

    elif owner_user:
        # Strict check for owner user type
        if getattr(owner_user, "tipo_chat", "") == "municipio":
            is_municipio = True
        # Explicit exclusion for known pyme types
        elif getattr(owner_user, "tipo_chat", "") in ["pyme", "empresa", "comercio"]:
            return None

    # Fallback to global context only if no explicit context was decisive
    if not is_municipio and not tenant_profile and not owner_user:
        if hasattr(g, "tenant_profile") and g.tenant_profile:
            if getattr(g.tenant_profile, "tipo", "") == "municipio":
                is_municipio = True
            elif getattr(g.tenant_profile, "slug", "") in ["municipio", "junin"]:
                is_municipio = True
        elif hasattr(g, "owner_user") and g.owner_user:
            if getattr(g.owner_user, "tipo_chat", "") == "municipio":
                is_municipio = True

    if not is_municipio:
        return None

    promo = get_active_promo(
        owner_user=owner_user,
        tenant_profile=tenant_profile,
        municipio_config=municipio_config,
    )
    if not promo:
        return None

    lines: list[str] = []
    headline = promo.get("headline")
    if headline:
        formatted_headline = headline
        if not formatted_headline.startswith("*"):
            formatted_headline = f"*{formatted_headline}"
        if not formatted_headline.endswith("*"):
            formatted_headline = f"{formatted_headline}*"
        lines.append(formatted_headline)

    tagline = promo.get("tagline")
    if tagline:
        lines.append(tagline)

    description = promo.get("description")
    if description:
        lines.append(description)

    link = promo.get("link")
    if link:
        lines.append(f"🔗 Más info: {link}")

    message_body = "\n".join(lines).strip()

    payload: dict[str, object] = {}
    if message_body:
        payload["message_body"] = message_body

    image_url = promo.get("image_url")
    if image_url:
        payload["image_url"] = image_url

    if link:
        payload["button"] = {
            "texto": promo.get("cta_text", "Más información"),
            "url": link,
            "type": "url",
        }

    return payload or None


def send_post_ticket_promo(ctx: dict, *, ticket_number: str | None = None, neighbor_name: str | None = None):
    """Legacy helper that sends the promo as a separate WhatsApp message."""

    if ctx.get("promo_sent_ts"):
        return None

    ctx["promo_sent_ts"] = time.time()

    # We must try to infer context from ctx to pass to build_ticket_promo_section
    # otherwise it will fail to determine if it's a municipality and return None (correct behavior)
    # or potentially leak if the fallback logic is weak (which we hardened above).

    # Extract owner_user if available in context
    owner_user_id = ctx.get("user_id") or ctx.get("pyme_id")
    owner_user = None
    if owner_user_id:
         # Need to avoid circular imports if possible, or use local import
         try:
             from models import User
             from extensions import db
             owner_user = db.session.get(User, owner_user_id)
         except Exception:
             pass

    promo_payload = build_ticket_promo_section(ticket_number, neighbor_name, owner_user=owner_user)
    if not promo_payload:
        return None

    message_body = promo_payload.get("message_body", "")
    response = {
        "message_body": message_body,
        "message_type": "interactive_buttons" if promo_payload.get("button") else "text",
    }
    button = promo_payload.get("button")
    if button:
        response["options_list"] = [button]
    if promo_payload.get("image_url"):
        response["image_url"] = promo_payload["image_url"]

    return response
