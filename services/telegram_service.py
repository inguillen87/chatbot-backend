import requests
import logging

logger = logging.getLogger(__name__)

def send_telegram_message(chat_id, text, bot_token=None):
    if not bot_token or not chat_id:
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=5)
        if not resp.ok:
            logger.error(f"[TELEGRAM] Error sending message: {resp.text}")
        return resp.ok
    except Exception as e:
        logger.error(f"[TELEGRAM] Exception sending message: {e}")
        return False
