from typing import Any, Dict, Optional


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
    if kind == "reclamo":
        return "\n".join(
            [
                "",
                "1. Menú",
                "2. Cancelar",
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
        lines.append(f"* *Nombre:* {nombre}")
    if cargo:
        lines.append(f"* *Cargo:* {cargo}")
    if telefono:
        lines.append(f"* *Teléfono:* {telefono}")
    if horario:
        lines.append(f"* *Horario:* {horario}")
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
                f"• *Ver mi Ticket:* {link}",
            ]
        )

    extra = ""
    if info_url:
        extra = f"\n\n🌐 *Más información:* {info_url}"

    contact_section = _render_contact_section(contacto_especializado)
    promo_section = f"\n\n{promo_text}" if promo_text else ""

    body_text = (
        f"{resumen}{seguimiento}{contact_section}{promo_section}{extra}{_build_menu_text(kind)}"
    )

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
    )
