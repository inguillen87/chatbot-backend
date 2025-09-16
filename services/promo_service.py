import time
from datetime import datetime
from typing import Optional, Dict, Any

PUNTO_LIMPIO_URL = "https://www.juninmendoza.gov.ar/punto-limpio"

PROMOTIONS = [
    {
        "key": "punto_limpio",
        "title": "¿Sabías que estamos trabajando para una Junín más limpia? ♻️",
        "lines": [
            "Conocé nuestra planta de recolección, reciclaje y elaboración de productos sustentables.",
            "Ladrillos, tejas, postes, mangueras, impresión 3D, luminarias LED y paneles solares.",
        ],
        "url": PUNTO_LIMPIO_URL,
        "cta_label": "Más info",
        "button_text": "♻️ Punto Limpio",
        "image_url": "https://www.facebook.com/photo/?fbid=1207006144791333&set=a.311633040995319",
    },
]


def _select_promo(now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Return the promotion that should be displayed for the current week."""

    if not PROMOTIONS:
        return None

    now = now or datetime.utcnow()
    week_index = now.isocalendar()[1] % len(PROMOTIONS)
    return PROMOTIONS[week_index]


def get_ticket_promo(context: Optional[Dict[str, Any]] = None, *, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    """Build the promotional payload attached to the ticket confirmation.

    The rotation is weekly: once new promotions are added to ``PROMOTIONS``
    they will cycle automatically.  ``context`` is optional and can be used to
    persist metadata if needed in the future.
    """

    promo = _select_promo(now=now)
    if not promo:
        return None

    lines = [promo.get("title", "").strip()]
    lines.extend(promo.get("lines", []))

    url = promo.get("url")
    if url:
        cta_label = promo.get("cta_label", "Más info")
        lines.append(f"{cta_label}: {url}")

    message_body = "\n".join(filter(None, lines)).strip()

    payload: Dict[str, Any] = {"message_body": message_body}
    if url and promo.get("button_text"):
        payload["button"] = {
            "texto": promo["button_text"],
            "url": url,
            "type": "url",
        }
    if promo.get("image_url"):
        payload["image_url"] = promo["image_url"]

    if context is not None:
        promo_state = context.setdefault("promo_state", {})
        promo_state["last_sent_key"] = promo.get("key")

    return payload


def send_post_ticket_promo(ctx: dict):
    """Send a promotional message after ticket creation if not already sent."""

    if ctx.get("promo_sent_ts"):
        return None

    promo_payload = get_ticket_promo(ctx)
    if not promo_payload:
        return None

    ctx["promo_sent_ts"] = time.time()
    return {
        "message_body": promo_payload["message_body"],
        "message_type": "text",
    }
