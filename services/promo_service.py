import time

PUNTO_LIMPIO_URL = "https://www.juninmendoza.gov.ar/punto-limpio"
OBRAS_URL = "https://www.juninmendoza.gov.ar/obras"


def send_post_ticket_promo(ctx: dict):
    """Send a promotional message after ticket creation if not already sent.

    Returns a payload with links to municipal initiatives or ``None`` if the
    promotion was recently delivered in this conversation. The timestamp is
    stored in ``ctx['promo_sent_ts']`` to avoid sending duplicates.
    """
    if ctx.get("promo_sent_ts"):
        return None
    ctx["promo_sent_ts"] = time.time()
    return {
        "message_body": (
            f"♻️ Junín Punto Limpio: {PUNTO_LIMPIO_URL}\n"
            f"📰 Obras y novedades: {OBRAS_URL}"
        ),
        "message_type": "text",
    }
