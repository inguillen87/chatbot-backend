from typing import Any, Dict, Optional


def _build_menu_text(kind: str) -> str:
    if kind == "sugerencia":
        first = "Hacer otra sugerencia"
    elif kind == "reclamo":
        first = "Hacer otro reclamo"
    else:
        first = "Hacer otro"
    return "\n".join(
        [
            "",
            "Opciones:",
            f"1) {first}",
            "2) Menú principal",
            "3) Cancelar",
        ]
    )


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
    name = nombre or "Vecino/a"
    ticket_line = f"• Ticket: {ticket_nro}" if ticket_nro else ""
    lines = [
        f"✅ ¡{kind.capitalize()} recibido/a, {name}!",
        "",
        "📄 Resumen:",
        ticket_line,
        f"• Categoría: {categoria}" if categoria else "",
        f"• Dirección: {direccion}" if direccion else "",
        f"• Descripción: {descripcion}" if descripcion else "",
        f"• DNI: {dni}" if dni else "",
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
                "🔗 Seguimiento:",
                f"• PIN: {consulta_pin}" if consulta_pin else "",
                f"• Ver mi Ticket: {link}",
            ]
        )

    extra = ""
    if info_url:
        extra = f"\n\n🌐 Más información: {info_url}"

    body_text = f"{resumen}{seguimiento}{extra}{_build_menu_text(kind)}"

    return {
        "body_text": body_text,
        "media_url": promo_image_url,
    }
