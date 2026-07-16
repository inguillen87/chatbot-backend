from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import services.notifications as notifications


def test_send_ticket_whatsapp_dispatches_only_when_provider_is_configured():
    app = Flask(__name__)
    ticket = SimpleNamespace(id=42, tenant_id=7)

    with app.app_context(), patch.object(notifications, "_log_dispatch") as dispatch:
        notifications.send_ticket_whatsapp(ticket, "ticket_updated")
        dispatch.assert_called_once_with(
            "whatsapp",
            ticket,
            "ticket_updated",
            ok=False,
        )

        dispatch.reset_mock()
        app.config["WHATSAPP_PROVIDER"] = "twilio"
        notifications.send_ticket_whatsapp(ticket, "ticket_updated")
        dispatch.assert_called_once_with("whatsapp", ticket, "ticket_updated")
