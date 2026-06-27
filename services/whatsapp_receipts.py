import json
from typing import Any, Dict, Optional


CLAIM_CREATED_TEMPLATE_NAME = "chatboc_gov_claim_created_v2"


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
        ticket_id_numeric = str(ticket_nro).replace("M-", "").replace("S-", "")
        link = (
            f"{base_chat_url.rstrip('/')}/{ticket_id_numeric}?pin={consulta_pin}"
            if consulta_pin
            else f"{base_chat_url.rstrip('/')}/{ticket_id_numeric}"
        )
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
) -> Dict[str, Any]:
    """Build a Twilio Content pre-message for a municipal claim receipt.

    The approved v2 template has a CTA at /t/{{2}}. We send {{2}} as
    chat/<ticket>?pin=<pin> and the frontend redirects that route to the
    public ticket view, keeping the approved template useful without sending a
    broken tracking button.
    """

    ticket_code = str(ticket_nro or "").strip()
    ticket_numeric = ticket_code.replace("M-", "").replace("S-", "")
    pin_value = str(consulta_pin or "").strip()
    tracking_path = f"chat/{ticket_numeric}" if ticket_numeric else "chat"
    if pin_value:
        tracking_path = f"{tracking_path}?pin={pin_value}"

    base_url = str(base_chat_url or "https://www.chatboc.ar/chat").rstrip("/")
    tracking_url = f"{base_url}/{ticket_numeric}" if ticket_numeric else base_url
    if pin_value:
        tracking_url = f"{tracking_url}?pin={pin_value}"

    body_lines = [
        f"Reclamo registrado: {ticket_code or 'pendiente'}",
        f"Categoria: {categoria or 'General'}",
        f"Seguimiento: {tracking_url}",
    ]
    if direccion:
        body_lines.append(f"Ubicacion: {direccion}")
    if nombre:
        body_lines.append("Tu mensaje queda asociado al expediente para seguimiento.")

    return {
        "channels": ["whatsapp"],
        "template_name": CLAIM_CREATED_TEMPLATE_NAME,
        "variables": {
            "1": ticket_code,
            "2": tracking_path,
        },
        "body": "\n".join(body_lines),
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
