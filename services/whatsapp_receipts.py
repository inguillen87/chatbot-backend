import json
from typing import Any, Dict, Optional

from services.ticket_utils import build_claim_tracking_url


CLAIM_CREATED_TEMPLATE_NAME = "chatboc_gov_claim_created_v2"
CLAIM_CREATED_TEMPLATE_VARIABLES = {
    "1": "claim_code",
    "2": "tracking_path",
}


def _normalize_ticket_code(ticket_nro: Any) -> tuple[str, str]:
    ticket_code = str(ticket_nro or "").strip()
    ticket_numeric = ticket_code
    if ticket_numeric.upper().startswith(("M-", "S-")):
        ticket_numeric = ticket_numeric[2:]
    return ticket_code, ticket_numeric


def build_claim_created_followup_text(consulta_pin: Optional[str]) -> str:
    """Return the short, non-duplicative companion to the claim receipt.

    The template (or its plain-text fallback) owns the public claim code and
    tracking link.  This companion only exposes the consultation PIN and tells
    the citizen how to keep adding evidence to the same ticket, avoiding two
    full receipts for one confirmation.
    """

    pin_value = str(consulta_pin or "").strip()
    lines = []
    if pin_value:
        lines.append(f"🔐 Guardá tu PIN de consulta: *{pin_value}*")
    lines.append(
        "Respondé a este chat con una foto, un audio o un comentario y lo "
        "vamos a asociar al mismo reclamo."
    )
    return "\n".join(lines)


def build_claim_replay_text(
    *,
    ticket_nro: str,
    consulta_pin: Optional[str],
    tracking_url: Optional[str],
) -> str:
    """Render a complete single-message receipt for an idempotent replay."""

    lines = [
        "✅ Este reclamo ya estaba registrado; no generamos otro ticket.",
        f"*Código:* {str(ticket_nro or '').strip() or 'Pendiente'}",
    ]
    pin_value = str(consulta_pin or "").strip()
    if pin_value:
        lines.append(f"*PIN:* {pin_value}")
    if tracking_url:
        lines.append(f"*Seguimiento:* {str(tracking_url).strip()}")
    lines.append(
        "Podés responder con una foto, un audio o un comentario; lo vamos a "
        "asociar al mismo reclamo."
    )
    return "\n".join(lines)


def _format_business_hours(value: Any) -> str:
    if value is None:
        return ""
    parsed = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ""
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return stripped
    if isinstance(parsed, list):
        items = []
        for row in parsed:
            if not isinstance(row, dict):
                continue
            dia = str(row.get("dia") or row.get("day") or "").strip()
            if not dia:
                continue
            cerrado = bool(row.get("cerrado"))
            abre = str(row.get("abre") or "").strip()
            cierra = str(row.get("cierra") or "").strip()
            if cerrado or (not abre and not cierra):
                items.append(f"{dia}: cerrado")
            elif abre and cierra:
                items.append(f"{dia}: {abre}-{cierra}")
            else:
                items.append(f"{dia}: horario a confirmar")
        return " | ".join(items)
    return str(parsed)


def _build_menu_text(kind: str) -> str:
    if kind == "sugerencia":
        return "\n".join(
            [
                "",
                "1. Hacer otra sugerencia",
                "2. Menú",
                "3. Cancelar",
            ]
        )
    return "\n".join(
        [
            "",
            "1. Menú",
            "2. Cancelar",
        ]
    )


def _render_contact_section(contacto: Optional[Dict[str, Any]]) -> str:
    if not isinstance(contacto, dict):
        return ""
    nombre = contacto.get("nombre")
    cargo = contacto.get("cargo") or contacto.get("titulo")
    telefono = contacto.get("telefono")
    horario = contacto.get("horario")
    if not any([nombre, cargo, telefono, horario]):
        return ""
    lines = ["", "📞 *Contacto para seguimiento:*"]
    if nombre:
        lines.append(f"• *Nombre:* {nombre}")
    if cargo:
        lines.append(f"• *Cargo:* {cargo}")
    if telefono:
        lines.append(f"• *Teléfono:* {telefono}")
    if horario:
        horario_legible = _format_business_hours(horario)
        if horario_legible:
            lines.append(f"• *Horario:* {horario_legible}")
    return "\n".join(lines)


def render_ticket_whatsapp(
    *,
    kind: str,
    nombre: str,
    ticket_nro: str,
    categoria: str,
    descripcion: str,
    direccion: Optional[str] = None,
    dni: Optional[str] = None,
    consulta_pin: Optional[str] = None,
    base_chat_url: str = "https://www.chatboc.ar/chat",
    promo_image_url: Optional[str] = None,
    promo_text: Optional[str] = None,
    contacto_especializado: Optional[Dict[str, Any]] = None,
    info_url: Optional[str] = None,
    include_menu: bool = True,
) -> Dict[str, Any]:
    name = nombre or "Vecino/a"
    kind_label = (
        "Reclamo"
        if kind == "reclamo"
        else "Sugerencia"
        if kind == "sugerencia"
        else kind.capitalize()
    )
    ticket_line = f"• *Ticket:* {ticket_nro}" if ticket_nro else ""
    lines = [
        f"✅ *¡{kind_label} recibido{'' if kind == 'reclamo' else 'a'}, {name}!*",
        "",
        "📄 *Resumen:*",
        ticket_line,
        f"• *Categoría:* {categoria}" if categoria else "",
        f"• *Dirección:* {direccion}" if direccion else "",
        f"• *Descripción:* {descripcion}" if descripcion else "",
        f"• *DNI:* {dni}" if dni else "",
    ]

    resumen = "\n".join([line for line in lines if line])

    seguimiento = ""
    if ticket_nro:
        link = build_claim_tracking_url(base_chat_url, ticket_nro, consulta_pin)
        seguimiento = "\n".join(
            [
                "",
                "🔗 *Seguimiento:*",
                f"• *PIN:* {consulta_pin}" if consulta_pin else "",
                f"• *Ver mi ticket:* {link}",
            ]
        )

    extra = ""
    if info_url:
        extra = f"\n\n🌐 *Más información:* {info_url}"

    contact_section = _render_contact_section(contacto_especializado)
    promo_section = f"\n\n{promo_text}" if promo_text else ""

    menu_text = _build_menu_text(kind) if include_menu else ""
    body_text = f"{resumen}{seguimiento}{contact_section}{promo_section}{extra}{menu_text}"

    return {
        "body_text": body_text,
        "media_url": promo_image_url,
    }


def build_claim_created_template_pre_message(
    *,
    ticket_nro: str,
    categoria: Optional[str],
    consulta_pin: Optional[str],
    base_chat_url: str = "https://www.chatboc.ar/chat",
    nombre: Optional[str] = None,
    direccion: Optional[str] = None,
    within_24h_window: bool = False,
) -> Dict[str, Any]:
    """Build a Twilio Content pre-message for a municipal claim receipt.

    The approved v2 template has a CTA at /t/{{2}}. Until that template is
    replaced in Meta/Twilio, {{2}} remains chat/<ticket>#pin=<pin> because the
    frontend redirects /t/chat/<ticket> to the public tracking page. The body
    and all backend metadata use /tracking/claim directly.
    """

    ticket_code, ticket_numeric = _normalize_ticket_code(ticket_nro)
    pin_value = str(consulta_pin or "").strip()
    tracking_path = f"chat/{ticket_numeric}" if ticket_numeric else "chat"
    if pin_value:
        tracking_path = f"{tracking_path}#pin={pin_value}"

    tracking_url = build_claim_tracking_url(base_chat_url, ticket_numeric, pin_value) or str(
        base_chat_url or "https://www.chatboc.ar"
    ).rstrip("/")

    # Keep the operational receipt compact and free of citizen PII.  ``nombre``
    # and ``direccion`` remain accepted for backwards compatibility, but are
    # deliberately excluded from both the template fallback and its metadata.
    body_lines = [
        "✅ *Reclamo registrado*",
        f"*Código:* {ticket_code or 'Pendiente'}",
        f"*Seguimiento:* {tracking_url}",
    ]

    variables = {
        "1": ticket_code,
        "2": tracking_path,
    }

    return {
        "channels": ["whatsapp"],
        "template_name": CLAIM_CREATED_TEMPLATE_NAME,
        "variables": variables,
        "content_variables": variables,
        "body": "\n".join(body_lines),
        "metadata": {
            # Callers must explicitly prove the active WhatsApp window.  The
            # default is fail-closed so proactive sends cannot masquerade as
            # an inbound reply to bypass tenant template policy.
            "within_24h_window": bool(within_24h_window),
            "operational_event": "municipal_claim_created",
            "contains_citizen_pii": False,
        },
        "fallback": {
            "mode": "plain_text",
            "trigger": "template_missing_pending_or_unapproved",
            "body": "\n".join(body_lines),
            "inside_24h_allowed": True,
        },
        "template_contract": {
            "contract_version": "whatsapp.operational_template_contract.v1",
            "id": "gov_claim_created",
            "friendly_name": CLAIM_CREATED_TEMPLATE_NAME,
            "variables": CLAIM_CREATED_TEMPLATE_VARIABLES,
            "receipt_contract": {
                "kind": "municipal_claim_receipt",
                "tracking_required": True,
                "pin_supported": True,
                "contains_citizen_pii": False,
                "tts_allowed": False,
                "surface_role": "primary_receipt",
                "pre_message_key": "_twilio_pre_messages",
            },
        },
        "next_actions": ["track_claim", "attach_photo_or_audio", "reply_with_comment"],
        "qa_cases": ["junin_texto_reclamo", "junin_confirmacion_reclamo"],
    }


def render_order_whatsapp(
    *,
    nombre: str,
    nro_pedido: str,
    monto_total: float,
    items_text: str,
    direccion: Optional[str] = None,
    link_seguimiento: Optional[str] = None,
    promo_image_url: Optional[str] = None,
    contacto_info: Optional[str] = None,
) -> Dict[str, Any]:
    name = nombre or "Cliente"
    lines = [
        f"✅ *¡Pedido confirmado, {name}!*",
        "",
        f"🆔 N°: *{nro_pedido}*",
        "📦 *Resumen del pedido:*",
        items_text,
        "",
        f"💰 *Total: ${monto_total:,.2f}*",
    ]

    if direccion:
        lines.append(f"📍 *Entrega:* {direccion}")

    if link_seguimiento:
        lines.append(f"\n🔗 *Seguimiento:* {link_seguimiento}")

    if contacto_info:
        lines.append(f"\n📞 *Contacto:* {contacto_info}")

    lines.append("\nGracias por tu compra. Te avisaremos cuando salga en camino.")

    body_text = "\n".join(lines)

    return {
        "body_text": body_text,
        "media_url": promo_image_url,
    }


def build_ticket_receipt(
    *,
    kind: str,
    nombre: str,
    ticket_nro: str,
    categoria: str,
    descripcion: str,
    direccion: Optional[str] = None,
    dni: Optional[str] = None,
    consulta_pin: Optional[str] = None,
    base_chat_url: str = "https://www.chatboc.ar/chat",
    promo_image_url: Optional[str] = None,
    info_url: Optional[str] = None,
    include_menu: bool = True,
) -> Dict[str, Any]:
    return render_ticket_whatsapp(
        kind=kind,
        nombre=nombre,
        ticket_nro=ticket_nro,
        categoria=categoria,
        descripcion=descripcion,
        direccion=direccion,
        dni=dni,
        consulta_pin=consulta_pin,
        base_chat_url=base_chat_url,
        promo_image_url=promo_image_url,
        promo_text=None,
        contacto_especializado=None,
        info_url=info_url,
        include_menu=include_menu,
    )
