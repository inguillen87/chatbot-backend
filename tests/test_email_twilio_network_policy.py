import logging
from unittest.mock import MagicMock, patch

from flask import Flask

from services import email_service


def _offline_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        TWILIO_ACCOUNT_SID="AC-private-account",
        TWILIO_AUTH_TOKEN="private-auth-token",
        TWILIO_PHONE_NUMBER="+15550000001",
        TWILIO_WHATSAPP_NUMBER="whatsapp:+15550000001",
    )
    return app


def test_sms_blocks_before_twilio_client_and_redacts_payload(caplog):
    app = _offline_app()
    caplog.set_level(logging.INFO)

    with app.app_context(), patch.object(email_service, "Client") as constructor:
        accepted = email_service.enviar_sms(
            "+15550000002",
            "mensaje privado de una vecina",
        )

    assert accepted is False
    constructor.assert_not_called()
    assert "LLMProviderNetworkDisabledError" in caplog.text
    assert "+15550000002" not in caplog.text
    assert "mensaje privado" not in caplog.text
    assert "private-auth-token" not in caplog.text


def test_whatsapp_blocks_before_twilio_client_and_redacts_media(caplog):
    app = _offline_app()
    caplog.set_level(logging.INFO)

    with app.app_context(), patch.object(email_service, "Client") as constructor:
        accepted = email_service.enviar_whatsapp(
            "+15550000002",
            "reclamo privado",
            ["https://media.example/private-photo.jpg?token=secret"],
        )

    assert accepted is False
    constructor.assert_not_called()
    assert "LLMProviderNetworkDisabledError" in caplog.text
    assert "+15550000002" not in caplog.text
    assert "reclamo privado" not in caplog.text
    assert "private-photo" not in caplog.text
    assert "private-auth-token" not in caplog.text


def test_sms_mock_transport_is_preserved_with_explicit_opt_in(caplog):
    app = _offline_app()
    app.config["TWILIO_ALLOW_NETWORK_IN_TESTS"] = True
    caplog.set_level(logging.INFO)
    fake_client = MagicMock()

    with app.app_context(), patch.object(
        email_service,
        "Client",
        return_value=fake_client,
    ) as constructor:
        accepted = email_service.enviar_sms(
            "+15550000002",
            "mensaje privado de una vecina",
        )

    assert accepted is True
    constructor.assert_called_once_with("AC-private-account", "private-auth-token")
    fake_client.messages.create.assert_called_once_with(
        body="mensaje privado de una vecina",
        from_="+15550000001",
        to="+15550000002",
    )
    assert "+15550000002" not in caplog.text
    assert "mensaje privado" not in caplog.text
    assert "private-auth-token" not in caplog.text
