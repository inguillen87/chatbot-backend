from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from services.voice_handler import initiate_outbound_call


def _app():
    app = Flask(__name__)
    app.config.update(
        TWILIO_ACCOUNT_SID="AC-test",
        TWILIO_AUTH_TOKEN="auth-test",
        BACKEND_URL="https://api.chatboc.ar",
        # A configured WhatsApp sender must not replace the explicit voice ID.
        TWILIO_PHONE_NUMBER="whatsapp:+14155550123",
    )
    return app


@patch("services.voice_handler.Client")
def test_outbound_transport_keeps_explicit_voice_caller_and_status_callback(mock_client):
    mock_client.return_value.calls.create.return_value = SimpleNamespace(sid="CA123")

    with _app().app_context():
        accepted = initiate_outbound_call(
            "+5492613168608",
            "+17432643718",
            chat_session_id="session 42",
        )

    assert accepted is True
    mock_client.return_value.calls.create.assert_called_once_with(
        to="+5492613168608",
        from_="+17432643718",
        url=(
            "https://api.chatboc.ar/twilio/voice/inbound"
            "?chat_session_id=session+42"
        ),
        method="POST",
        status_callback="https://api.chatboc.ar/voice/status",
        status_callback_method="POST",
        status_callback_event=["initiated", "ringing", "answered", "completed"],
    )


@patch("services.voice_handler.Client")
def test_outbound_transport_rejects_non_e164_before_provider_send(mock_client):
    with _app().app_context():
        accepted = initiate_outbound_call(
            "2613168608",
            "+17432643718",
        )

    assert accepted is False
    mock_client.return_value.calls.create.assert_not_called()
