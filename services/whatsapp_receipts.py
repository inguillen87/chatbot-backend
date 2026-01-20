<<<<<<< Updated upstream
import os
from typing import Any, Dict, Optional

from utils.url_utils import public_url

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://api.chatboc.ar")


def _format_horario(horario: Any) -> str:
    if not horario:
        return ""
    if isinstance(horario, str):
        return horario.strip()
    if not isinstance(horario, list):
        return ""
    lines = []
    for item in horario:
        if not isinstance(item, dict):
            continue
        dia = item.get("dia", "")
        abre = item.get("abre", "")
        cierra = item.get("cierra", "")
        cerrado = item.get("cerrado", False)
        if cerrado:
            lines.append(f"• {dia}: cerrado")
        else:
            lines.append(f"• {dia}: {abre} a {cierra}")
    return "\n".join([line for line in lines if line])

=======
from typing import Any, Dict, Optional

>>>>>>> Stashed changes

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
<<<<<<< Updated upstream
    lines = ["", "📞 *Contacto para seguimiento:*"]
    if nombre:
        lines.append(f"• *Nombre:* {nombre}")
    if cargo:
        lines.append(f"• *Cargo:* {cargo}")
    if telefono:
        lines.append(f"• *Teléfono:* {telefono}")
    horario_txt = _format_horario(horario)
    if horario_txt:
        if "\n" in horario_txt:
            lines.append("🕘 *Horario:*")
            lines.append(horario_txt)
        else:
            lines.append(f"• *Horario:* {horario_txt}")
=======
    lines = ["", "📞 Contacto para seguimiento:"]
    if nombre:
        lines.append(f"* Nombre: {nombre}")
    if cargo:
        lines.append(f"* Cargo: {cargo}")
    if telefono:
        lines.append(f"* Teléfono: {telefono}")
    if horario:
        lines.append(f"* Horario: {horario}")
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
    kind_label = "Reclamo" if kind == "reclamo" else "Sugerencia" if kind == "sugerencia" else kind.capitalize()
    ticket_line = f"• *Ticket:* {ticket_nro}" if ticket_nro else ""
    lines = [
        f"✅ ¡{kind_label} recibido, {name}!",
        "",
        "📄 *Resumen:*",
        ticket_line,
        f"• *Categoría:* {categoria}" if categoria else "",
        f"• *Dirección:* {direccion}" if direccion else "",
        f"• *Descripción:* {descripcion}" if descripcion else "",
        f"• *DNI:* {dni}" if dni else "",
=======
    kind_label = (
        "Reclamo"
        if kind == "reclamo"
        else "Sugerencia"
        if kind == "sugerencia"
        else kind.capitalize()
    )
    ticket_line = f"• Ticket: {ticket_nro}" if ticket_nro else ""
    lines = [
        f"✅ ¡{kind_label} recibido{'' if kind == 'reclamo' else 'a'}, {name}!",
        "",
        "📄 Resumen:",
        ticket_line,
        f"• Categoría: {categoria}" if categoria else "",
        f"• Dirección: {direccion}" if direccion else "",
        f"• Descripción: {descripcion}" if descripcion else "",
        f"• DNI: {dni}" if dni else "",
>>>>>>> Stashed changes
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
<<<<<<< Updated upstream
        seguimiento_lines = [
            "",
            "🔗 *Seguimiento:*",
        ]
        if consulta_pin:
            seguimiento_lines.append(f"• *PIN:* {consulta_pin}")
        seguimiento_lines.append(f"• *Ver mi Ticket:* {link}")
        seguimiento = "\n".join(seguimiento_lines)
=======
        seguimiento = "\n".join(
            [
                "",
                "🔗 Seguimiento:",
                f"• PIN: {consulta_pin}" if consulta_pin else "",
                f"• Ver mi Ticket: {link}",
            ]
        )
>>>>>>> Stashed changes

    extra = ""
    if info_url:
        extra = f"\n\n🌐 Más información: {info_url}"

    contact_section = _render_contact_section(contacto_especializado)
    promo_section = f"\n\n{promo_text}" if promo_text else ""

    body_text = (
        f"{resumen}{seguimiento}{contact_section}{promo_section}{extra}{_build_menu_text(kind)}"
    )

    return {
        "body_text": body_text,
<<<<<<< Updated upstream
        "media_url": public_url(promo_image_url, PUBLIC_BASE_URL),
=======
        "media_url": promo_image_url,
>>>>>>> Stashed changes
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
