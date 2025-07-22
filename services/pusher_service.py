import pusher
from flask import current_app

def get_pusher_client():
    """Initializes and returns a Pusher client instance."""
    return pusher.Pusher(
        app_id=current_app.config["PUSHER_APP_ID"],
        key=current_app.config["PUSHER_KEY"],
        secret=current_app.config["PUSHER_SECRET"],
        cluster=current_app.config["PUSHER_CLUSTER"],
        ssl=True
    )

def trigger_notification(channel, event, data):
    """Triggers a notification to the specified channel."""
    pusher_client = get_pusher_client()
    pusher_client.trigger(channel, event, data)
