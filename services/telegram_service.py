"""Telegram Notification Service."""
import requests
import logging

logger = logging.getLogger(__name__)

def send_telegram_message(chat_id: str, text: str, bot_token: str) -> bool:
    """Sends a message to a Telegram chat."""
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text}
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"[TELEGRAM] Failed to send message to {chat_id}: {e}")
        return False
