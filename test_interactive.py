import os
import json
from twilio.rest import Client


def main() -> None:
    """Send a sample interactive WhatsApp message using Twilio."""
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    to_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")
    from_number = "whatsapp:+14155238886"  # Twilio sandbox number

    client = Client(account_sid, auth_token)

    interactive_message = {
        "type": "button",
        "body": {"text": "This is a test message with a button."},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {"id": "test-button-1", "title": "Test Button"},
                }
            ]
        },
    }

    try:
        message = client.messages.create(
            from_=from_number,
            to=to_number,
            body="This is the fallback body.",
            persistent_action=[f"whatsapp:{json.dumps(interactive_message)}"],
        )
        print(f"Message sent successfully! SID: {message.sid}")
    except Exception as e:  # pragma: no cover - best effort script
        print(f"Error sending message: {e}")


if __name__ == "__main__":  # pragma: no cover - manual utility
    main()
