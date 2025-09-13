import os
from twilio.rest import Client
import json

# Your Account SID and Auth Token from console.twilio.com
account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
to_number = os.environ.get("TWILIO_WHATSAPP_NUMBER") # Sending to the same number for testing
from_number = "whatsapp:+14155238886" # Twilio sandbox number

client = Client(account_sid, auth_token)

interactive_message = {
    "type": "button",
    "header": {
        "type": "text",
        "text": "Test Header"
    },
    "body": {
        "text": "This is a test message with a button."
    },
    "action": {
        "buttons": [
            {
                "type": "reply",
                "reply": {
                    "id": "test-button-1",
                    "title": "Test Button"
                }
            }
        ]
    }
}

try:
    message = client.messages.create(
        from_=from_number,
        to=to_number,
        body="This is the fallback body.", # Fallback body
        interactive=interactive_message
    )
    print(f"Message sent successfully! SID: {message.sid}")
except Exception as e:
    print(f"Error sending message: {e}")
