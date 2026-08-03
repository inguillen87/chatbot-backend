import logging
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask, g

from routes import whatsapp_rules, whatsapp_webhook
from services import (
    tasks,
    tenant_twilio_messaging,
    twilio_tech_provider,
    voice_handler,
    whatsapp_inbound_worker,
)
from services.llm_provider_network_policy import ProviderNetworkDisabledError
from services.tenant_twilio_messaging import PreparedTenantTwilioMessage
from services.voice_stream_service import VoiceStreamService


@pytest.fixture(autouse=True)
def _twilio_offline_by_default(monkeypatch, caplog):
    monkeypatch.setenv("TESTING", "1")
    monkeypatch.delenv("TWILIO_ALLOW_NETWORK_IN_TESTS", raising=False)
    caplog.set_level(logging.INFO)
    whatsapp_webhook._reset_parent_twilio_client_for_tests()
    yield
    whatsapp_webhook._reset_parent_twilio_client_for_tests()


def _offline_app(**extra_config):
    app = Flask(__name__)
    app.config.update(TESTING=True, **extra_config)
    return app


def test_tenant_sender_blocks_before_twilio_client_and_logs_no_payload(caplog):
    prepared = PreparedTenantTwilioMessage(
        account_sid="AC-private-account",
        auth_token="private-auth-token",
        provider_sender_id=7,
        params={
            "from_": "+15550000001",
            "to": "+15550000002",
            "body": "private-message-body",
        },
    )

    caplog.clear()
    with (
        patch.object(tenant_twilio_messaging, "Client") as constructor,
        pytest.raises(
            ProviderNetworkDisabledError,
            match="twilio_test_network_disabled",
        ),
    ):
        tenant_twilio_messaging.send_prepared_tenant_twilio_message(prepared)

    constructor.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "reason=test_network_disabled" in caplog.text
    assert "private-auth-token" not in caplog.text
    assert "private-message-body" not in caplog.text


def test_outbound_voice_blocks_before_twilio_client(caplog):
    app = _offline_app(
        TWILIO_ACCOUNT_SID="AC-private-account",
        TWILIO_AUTH_TOKEN="private-auth-token",
        BACKEND_URL="https://api.chatboc.test",
    )

    caplog.clear()
    with app.app_context(), patch.object(voice_handler, "Client") as constructor:
        accepted = voice_handler.initiate_outbound_call(
            "+5492613168608",
            "+17432643718",
        )

    assert accepted is False
    constructor.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "private-auth-token" not in caplog.text


def test_webhook_sender_blocks_before_hook_and_provider_send(caplog):
    app = _offline_app()
    client = MagicMock()
    provider_call_hook = MagicMock()

    caplog.clear()
    with app.app_context(), pytest.raises(
        ProviderNetworkDisabledError,
        match="twilio_test_network_disabled",
    ):
        whatsapp_webhook._send_twilio_message(
            client,
            from_="+15550000001",
            to="+15550000002",
            body="private-message-body",
            _chatboc_provider_call_hook=provider_call_hook,
        )

    provider_call_hook.assert_not_called()
    client.messages.create.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "private-message-body" not in caplog.text


def test_webhook_durable_collector_stays_offline_and_captures_message():
    app = _offline_app()
    collector = MagicMock()
    collector.capture.return_value = SimpleNamespace(effect_id=17)
    client = MagicMock()

    with app.app_context():
        g.whatsapp_outbound_collector = collector
        result = whatsapp_webhook._send_twilio_message(
            client,
            from_="whatsapp:+15550000001",
            to="whatsapp:+15550000002",
            body="durable-private-body",
        )

    assert result.effect_id == 17
    collector.capture.assert_called_once()
    client.messages.create.assert_not_called()


def test_webhook_media_download_blocks_before_http_and_redacts_url(caplog):
    app = _offline_app()

    caplog.clear()
    with (
        app.app_context(),
        patch.object(whatsapp_webhook.requests, "get") as request_get,
        pytest.raises(
            ProviderNetworkDisabledError,
            match="twilio_test_network_disabled",
        ),
    ):
        whatsapp_webhook._download_twilio_media(
            "https://api.twilio.test/private-media?token=secret",
            auth=("AC-private-account", "private-auth-token"),
            timeout=3.0,
            max_bytes=1024,
        )

    request_get.assert_not_called()
    assert "private-media" not in caplog.text
    assert "private-auth-token" not in caplog.text


def test_parent_webhook_client_is_lazy_and_not_constructed_offline(monkeypatch, caplog):
    app = _offline_app()
    monkeypatch.setattr(whatsapp_webhook, "TWILIO_ACCOUNT_SID", "AC-private-account")
    monkeypatch.setattr(whatsapp_webhook, "TWILIO_AUTH_TOKEN", "private-auth-token")
    whatsapp_webhook._reset_parent_twilio_client_for_tests()

    caplog.clear()
    with app.app_context(), patch.object(whatsapp_webhook, "Client") as constructor:
        assert whatsapp_webhook._get_parent_twilio_client() is None

    constructor.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "private-auth-token" not in caplog.text


def test_parent_webhook_client_constructs_once_with_explicit_mock_opt_in(monkeypatch):
    app = _offline_app(TWILIO_ALLOW_NETWORK_IN_TESTS=True)
    monkeypatch.setattr(whatsapp_webhook, "TWILIO_ACCOUNT_SID", "AC-mocked-account")
    monkeypatch.setattr(whatsapp_webhook, "TWILIO_AUTH_TOKEN", "mocked-auth-token")
    whatsapp_webhook._reset_parent_twilio_client_for_tests()
    fake_client = MagicMock()

    with app.app_context(), patch.object(
        whatsapp_webhook,
        "Client",
        return_value=fake_client,
    ) as constructor:
        first = whatsapp_webhook._get_parent_twilio_client()
        second = whatsapp_webhook._get_parent_twilio_client()

    assert first is fake_client
    assert second is fake_client
    constructor.assert_called_once_with("AC-mocked-account", "mocked-auth-token")


@pytest.mark.parametrize(
    ("helper", "kwargs", "request_method"),
    [
        (
            twilio_tech_provider._twilio_post_form,
            {
                "url": "https://api.twilio.test/private",
                "account_sid": "AC-private-account",
                "auth_token": "private-auth-token",
                "data": {"Body": "private-message-body"},
            },
            "post",
        ),
        (
            twilio_tech_provider._twilio_post_json,
            {
                "url": "https://api.twilio.test/private",
                "account_sid": "AC-private-account",
                "auth_token": "private-auth-token",
                "payload": {"friendly_name": "private-resource"},
            },
            "post",
        ),
        (
            twilio_tech_provider._twilio_get_json,
            {
                "url": "https://api.twilio.test/private",
                "account_sid": "AC-private-account",
                "auth_token": "private-auth-token",
            },
            "get",
        ),
    ],
)
def test_twilio_tech_provider_blocks_before_http(
    helper,
    kwargs,
    request_method,
    caplog,
):
    caplog.clear()
    with (
        patch.object(twilio_tech_provider.requests, request_method) as request_call,
        pytest.raises(
            ProviderNetworkDisabledError,
            match="twilio_test_network_disabled",
        ),
    ):
        helper(**kwargs)

    request_call.assert_not_called()
    assert "reason=test_network_disabled" in caplog.text
    assert "private-auth-token" not in caplog.text
    assert "private-message-body" not in caplog.text


def test_twilio_http_error_never_exposes_provider_payload(monkeypatch):
    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    response = MagicMock(status_code=400, text="private-provider-response")
    response.json.return_value = {
        "message": "private-provider-response",
        "auth_token": "private-auth-token",
    }

    with patch.object(
        twilio_tech_provider.requests,
        "post",
        return_value=response,
    ), pytest.raises(RuntimeError) as raised:
        twilio_tech_provider._twilio_post_form(
            url="https://api.twilio.test/private",
            account_sid="AC-private-account",
            auth_token="private-auth-token",
            data={"Body": "private-message-body"},
        )

    assert str(raised.value) == "twilio_api_error_status_400"
    assert "private-provider-response" not in str(raised.value)
    assert "private-auth-token" not in str(raised.value)


def test_voice_stream_end_call_blocks_before_twilio_client(caplog):
    app = _offline_app(
        TWILIO_ACCOUNT_SID="AC-private-account",
        TWILIO_AUTH_TOKEN="private-auth-token",
    )
    service = VoiceStreamService(MagicMock(), app=app)
    service.call_sid = "CA-private-call"

    caplog.clear()
    with app.app_context(), patch(
        "services.voice_stream_service.TwilioClient"
    ) as constructor:
        service._safe_end_call_twilio()

    constructor.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "CA-private-call" not in caplog.text
    assert "private-auth-token" not in caplog.text


def test_whatsapp_rules_blocks_client_constructor_and_content_request(caplog):
    app = _offline_app()
    credentials = SimpleNamespace(
        ready=True,
        scope="subaccount",
        account_sid="AC-private-account",
        auth_token="private-auth-token",
    )
    provider_client = MagicMock()

    caplog.clear()
    with app.app_context(), patch.object(
        whatsapp_rules,
        "_twilio_credentials",
        return_value=credentials,
    ), patch.object(whatsapp_rules, "Client") as constructor, pytest.raises(
        ProviderNetworkDisabledError,
        match="twilio_test_network_disabled",
    ):
        whatsapp_rules._twilio_client(SimpleNamespace(id=9))

    constructor.assert_not_called()

    with app.app_context(), pytest.raises(
        ProviderNetworkDisabledError,
        match="twilio_test_network_disabled",
    ):
        whatsapp_rules._twilio_json_request(
            provider_client,
            "POST",
            "https://content.twilio.test/private",
            payload={"body": "private-message-body"},
        )

    provider_client.request.assert_not_called()
    assert "reason=test_network_disabled" in caplog.text
    assert "private-auth-token" not in caplog.text
    assert "private-message-body" not in caplog.text


def test_image_task_sender_blocks_before_twilio_client(caplog):
    caplog.clear()
    with patch.object(tasks, "Client") as constructor, pytest.raises(
        ProviderNetworkDisabledError,
        match="twilio_test_network_disabled",
    ):
        tasks._send_image_analysis_twilio_message(
            account_sid="AC-private-account",
            auth_token="private-auth-token",
            from_number="whatsapp:+15550000001",
            to_number="whatsapp:+15550000002",
            body="private-message-body",
        )

    constructor.assert_not_called()
    assert "provider=twilio" in caplog.text
    assert "private-auth-token" not in caplog.text
    assert "private-message-body" not in caplog.text


def test_durable_worker_blocks_before_twilio_client_and_keeps_attempt_retryable():
    app = _offline_app(
        WHATSAPP_INBOUND_DURABILITY_MODE="queue",
        WHATSAPP_INBOUND_QUEUE_TENANT_IDS="17",
        WHATSAPP_INBOUND_HASH_SECRET="q" * 32,
        CHANNEL_SESSION_IDENTITY_MODE="enforce",
        CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1="i" * 32,
        WHATSAPP_INBOUND_LEASE_SECONDS=180,
    )
    claim = SimpleNamespace(
        attempt_id="attempt-private",
        tenant_id=17,
        lease_token="lease-private",
        payload={"from_": "whatsapp:+15550000001", "body": "private-body"},
    )
    credentials = SimpleNamespace(
        account_sid="AC-private-account",
        auth_token="private-auth-token",
    )
    fake_db = MagicMock()

    with (
        app.app_context(),
        patch.object(
            whatsapp_inbound_worker,
            "claim_next_whatsapp_outbound_attempt",
            return_value=claim,
        ),
        patch.object(
            whatsapp_inbound_worker,
            "_load_claim_scope",
            return_value=(None, None, credentials),
        ),
        patch.object(
            whatsapp_inbound_worker,
            "_status_callback_for_attempt",
            return_value="https://api.chatboc.test/status",
        ),
        patch.object(whatsapp_inbound_worker, "db", fake_db),
        patch.object(
            whatsapp_inbound_worker,
            "retry_whatsapp_outbound_attempt",
            return_value="retry_wait",
        ) as retry,
        patch.object(whatsapp_inbound_worker, "Client") as constructor,
    ):
        result = whatsapp_inbound_worker.dispatch_next_whatsapp_outbound_attempt(
            tenant_id=17
        )

    constructor.assert_not_called()
    retry.assert_called_once()
    assert result.status == "retry_wait"
    assert result.error_code == "LLMProviderNetworkDisabledError"
