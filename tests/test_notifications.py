from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

import services.notifications as notifications


def test_send_ticket_whatsapp_never_treats_provider_config_as_acceptance():
    app = Flask(__name__)
    ticket = SimpleNamespace(id=42, tenant_id=7)

    with app.app_context(), patch.object(notifications, "_log_dispatch") as dispatch:
        missing_provider = notifications.send_ticket_whatsapp(ticket, "ticket_updated")
        dispatch.assert_called_once_with(
            "whatsapp",
            ticket,
            "ticket_updated",
            ok=False,
        )
        assert missing_provider.accepted is False
        assert missing_provider.reason_code == "whatsapp_template_transport_unavailable"

        dispatch.reset_mock()
        app.config["WHATSAPP_PROVIDER"] = "twilio"
        configured_provider = notifications.send_ticket_whatsapp(ticket, "ticket_updated")
        dispatch.assert_called_once_with(
            "whatsapp",
            ticket,
            "ticket_updated",
            ok=False,
        )
        assert configured_provider.accepted is False
        assert configured_provider.provider_message_id is None
