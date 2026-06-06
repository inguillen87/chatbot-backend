"""
Renders a neutral JSON payload into a WhatsApp-compatible text message.
"""
from urllib.parse import quote

from services.response_formatter import repair_common_mojibake


def _clean(value) -> str:
    return repair_common_mojibake(value).strip()


def render(payload: dict) -> str:
    """
    Takes a neutral payload and returns a formatted string for WhatsApp.
    """
    if not isinstance(payload, dict):
        return "Error: payload no es un diccionario."

    parts = []

    # Title, Summary, and Body
    if payload.get("title"):
        parts.append(f"*{_clean(payload['title'])}*")
    if payload.get("summary"):
        parts.append(_clean(payload['summary']))
    if payload.get("message_body"):
        # Add a separator if title or summary already exists
        if payload.get("title") or payload.get("summary"):
            parts.append("-" * 20)
        parts.append(_clean(payload['message_body']))

    # Data items (for menus or news)
    if payload.get("data", {}).get("items"):
        item_parts = []
        # Check for news type to add date
        if payload.get("type") == "noticias":
            for item in payload["data"]["items"]:
                date_str = f" ({_clean(item.get('date'))})" if item.get('date') else ""
                item_parts.append(f"📰 *{_clean(item.get('title'))}*{date_str}\n   {_clean(item.get('url'))}")
        else: # Generic menu
            for item in payload["data"]["items"]:
                item_parts.append(f"{_clean(item.get('n') or '•')} {_clean(item.get('label'))}")
        if item_parts:
            parts.append("\n".join(item_parts))

    # Links
    if payload.get("links"):
        link_parts = ["\n*Links útiles:*"]
        for i, link in enumerate(payload["links"], 1):
            link_parts.append(f"{i}️⃣ {_clean(link.get('label'))}: {_clean(link.get('url'))}")
        parts.append("\n".join(link_parts))

    # Phones
    if payload.get("phones"):
        phone_parts = ["\n*Contacto directo:*"]
        for phone in payload["phones"]:
            number = _clean(phone.get('number'))
            number_clean = ''.join(filter(str.isdigit, number))
            whatsapp_link = ""
            if phone.get("whatsapp"):
                whatsapp_link = f" (WhatsApp: https://wa.me/{number_clean}?text=Hola)"
            phone_parts.append(f"• {_clean(phone.get('label'))}: {number}{whatsapp_link}")
        parts.append("\n".join(phone_parts))

    # Location
    if payload.get("location"):
        loc = payload["location"]
        loc_parts = []
        if loc.get('label') and loc.get('address'):
            loc_parts.append(f"\n*Dónde:* {_clean(loc.get('label'))} — {_clean(loc.get('address'))}")
        elif loc.get('address'):
             loc_parts.append(f"\n*Dónde:* {_clean(loc.get('address'))}")

        if loc.get("gmap"):
            loc_parts.append(f"🗺️ {_clean(loc.get('gmap'))}")
        if loc_parts:
            parts.append("\n".join(loc_parts))

    # Hours
    if payload.get("hours"):
        hour_parts = ["\n*Horarios:*"]
        for hour in payload["hours"]:
            hour_parts.append(f"• {_clean(hour.get('days'))}: {_clean(hour.get('time'))}")
        parts.append("\n".join(hour_parts))

    # Ticket info
    if payload.get("ticket"):
        ticket = payload["ticket"]
        ticket_parts = [f"\n*Ticket:* {_clean(ticket.get('id'))}"]
        if ticket.get("category"):
            ticket_parts.append(f"• Categoría: {_clean(ticket.get('category'))}")
        if ticket.get("address"):
            ticket_parts.append(f"• Dirección: {_clean(ticket.get('address'))}")
        if ticket.get("status_url"):
            ticket_parts.append(f"• Ver estado: {_clean(ticket.get('status_url'))}")
        parts.append("\n".join(ticket_parts))

    # CTAs (Acciones rápidas)
    if payload.get("cta"):
        cta_parts = ["\n*Acciones rápidas:*"]
        for cta in payload["cta"]:
            if cta.get("type") == "wa":
                to_clean = ''.join(filter(str.isdigit, _clean(cta.get('to'))))
                text_encoded = quote(_clean(cta.get('text')))
                cta_parts.append(f"• {_clean(cta.get('label'))}: https://wa.me/{to_clean}?text={text_encoded}")
            elif cta.get("type") == "tel":
                cta_parts.append(f"• {_clean(cta.get('label'))}: tel:{_clean(cta.get('to'))}")
            elif cta.get("type") == "url":
                cta_parts.append(f"• {_clean(cta.get('label'))}: {_clean(cta.get('url'))}")
        if len(cta_parts) > 1:
            parts.append("\n".join(cta_parts))

    # Fallback final
    parts.append("\n_Decí *menu* para volver._")

    # Clean up empty sections and join
    final_text = _clean("\n".join(filter(None, parts)))

    # Guard against empty messages (Twilio error 21619)
    # Check if the text is empty or only contains the menu fallback
    if not final_text or final_text == "_Decí *menu* para volver._":
        return "No pude encontrar una respuesta específica. ¿Querés intentar con otras palabras?\n\n_Decí *menu* para volver._"

    return final_text.replace('\n\n\n', '\n\n') # Clean up extra newlines
