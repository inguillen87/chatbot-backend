from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from flask import Flask
import pytest

from services.outbox_execution_budget import (
    OutboxExecutionBudgetExceeded,
    activate_outbox_execution_budget,
    install_outbox_database_timeout_hook,
    outbox_database_timeout_seconds,
    outbox_execution_budget_active,
    outbox_io_timeout_seconds,
    outbox_persistence_budget,
    outbox_persistence_budget_active,
    outbox_twilio_http_client,
)


def test_budget_is_inert_outside_reconciliation_and_scoped_inside_it():
    assert outbox_execution_budget_active() is False
    assert outbox_io_timeout_seconds(90) is None

    clock = MagicMock(return_value=100.0)
    with activate_outbox_execution_budget(
        deadline_monotonic=145.0,
        clock=clock,
    ):
        assert outbox_execution_budget_active() is True
        assert outbox_io_timeout_seconds(90) == 4.0

    assert outbox_execution_budget_active() is False
    assert outbox_io_timeout_seconds(90) is None


def test_budget_uses_remaining_runway_and_refuses_a_new_io_at_the_reserve():
    now = [137.0]
    with activate_outbox_execution_budget(
        deadline_monotonic=145.0,
        clock=lambda: now[0],
    ):
        assert outbox_io_timeout_seconds(30) == 2.0
        now[0] = 139.0
        with pytest.raises(
            OutboxExecutionBudgetExceeded,
            match="outbox_execution_budget_exhausted",
        ):
            outbox_io_timeout_seconds()


def test_persistence_mode_uses_real_runway_without_reopening_provider_io():
    now = [139.0]
    with activate_outbox_execution_budget(
        deadline_monotonic=145.0,
        clock=lambda: now[0],
    ):
        with pytest.raises(OutboxExecutionBudgetExceeded):
            outbox_io_timeout_seconds()

        with outbox_persistence_budget():
            assert outbox_persistence_budget_active() is True
            assert outbox_database_timeout_seconds() == 1.0
            with pytest.raises(OutboxExecutionBudgetExceeded):
                outbox_io_timeout_seconds()

            now[0] = 144.95
            with pytest.raises(OutboxExecutionBudgetExceeded):
                outbox_database_timeout_seconds()

    assert outbox_persistence_budget_active() is False
    assert outbox_database_timeout_seconds() is None


def test_postgresql_begin_hook_sets_local_statement_and_lock_limits():
    class FakeEngine:
        pass

    engine = FakeEngine()
    registered = []
    with patch(
        "services.outbox_execution_budget.event.listen",
        side_effect=lambda target, name, callback: registered.append(
            (target, name, callback)
        ),
    ):
        install_outbox_database_timeout_hook(engine)

    connection = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        exec_driver_sql=MagicMock(),
    )
    with activate_outbox_execution_budget(
        deadline_monotonic=145.0,
        clock=lambda: 100.0,
    ):
        begin_callback = next(
            callback for _target, name, callback in registered if name == "begin"
        )
        begin_callback(connection)

    assert [(target, name) for target, name, _callback in registered] == [
        (engine, "begin"),
        (engine, "before_cursor_execute"),
    ]
    assert connection.exec_driver_sql.call_args_list == [
        call("SET LOCAL lock_timeout = 2000"),
        call("SET LOCAL statement_timeout = 4000"),
    ]

    statement_callback = next(
        callback
        for _target, name, callback in registered
        if name == "before_cursor_execute"
    )
    with activate_outbox_execution_budget(
        deadline_monotonic=145.0,
        clock=lambda: 139.0,
    ), pytest.raises(OutboxExecutionBudgetExceeded):
        statement_callback(None, None, "SELECT 1", None, None, False)


def test_existing_postgresql_transaction_is_shortened_for_persistence():
    class FakeEngine:
        pass

    registered = []
    with patch(
        "services.outbox_execution_budget.event.listen",
        side_effect=lambda target, name, callback: registered.append(
            (target, name, callback)
        ),
    ):
        install_outbox_database_timeout_hook(FakeEngine())

    statement_callback = next(
        callback
        for _target, name, callback in registered
        if name == "before_cursor_execute"
    )
    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    cursor = MagicMock()
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 139.0,
        ),
        outbox_persistence_budget(),
    ):
        statement_callback(
            connection,
            cursor,
            "UPDATE domain_effect_outbox SET status = 'succeeded'",
            None,
            None,
            False,
        )

    assert cursor.execute.call_args_list == [
        call("SET LOCAL lock_timeout = 1000"),
        call("SET LOCAL statement_timeout = 1000"),
    ]


def test_twilio_transport_is_bounded_only_in_reconciliation():
    with patch(
        "twilio.http.http_client.TwilioHttpClient"
    ) as http_client_constructor:
        assert outbox_twilio_http_client() is None
        http_client_constructor.assert_not_called()

        with activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ):
            bounded_client = outbox_twilio_http_client()

    assert bounded_client is http_client_constructor.return_value
    http_client_constructor.assert_called_once_with(timeout=4.0)


def test_tenant_twilio_dispatch_injects_the_bounded_transport():
    import services.tenant_twilio_messaging as messaging

    prepared = messaging.PreparedTenantTwilioMessage(
        account_sid="AC" + ("1" * 32),
        auth_token="private-token",
        provider_sender_id=7,
        params={
            "body": "estado actualizado",
            "from_": "+15005550001",
            "to": "+15005550002",
        },
    )
    transport = object()
    provider_client = MagicMock()
    provider_client.messages.create.return_value = SimpleNamespace(
        sid="SM" + ("1" * 32)
    )
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(messaging, "require_provider_network"),
        patch.object(
            messaging,
            "outbox_twilio_http_client",
            return_value=transport,
        ),
        patch.object(messaging, "Client", return_value=provider_client) as client,
    ):
        result = messaging.send_prepared_tenant_twilio_message(prepared)

    assert result == "SM" + ("1" * 32)
    client.assert_called_once_with(
        prepared.account_sid,
        prepared.auth_token,
        http_client=transport,
    )


def test_whatsapp_outbound_dispatch_injects_the_bounded_transport():
    import services.whatsapp_inbound_worker as worker

    app = Flask(__name__)
    app.config["WHATSAPP_INBOUND_LEASE_SECONDS"] = 180
    claim = SimpleNamespace(
        attempt_id="attempt-1",
        tenant_id=17,
        lease_token="lease-1",
        payload={"from_": "whatsapp:+15005550001", "body": "respuesta"},
    )
    credentials = SimpleNamespace(account_sid="AC-test", auth_token="token")
    provider_message = SimpleNamespace(sid="SM-test", status="queued")
    transport = object()
    with (
        app.app_context(),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(worker, "_worker_tenant_ids", return_value=(17,)),
        patch.object(
            worker,
            "claim_next_whatsapp_outbound_attempt",
            return_value=claim,
        ),
        patch.object(
            worker,
            "_load_claim_scope",
            return_value=(None, None, credentials),
        ),
        patch.object(worker, "_status_callback_for_attempt", return_value=None),
        patch.object(worker, "require_provider_network"),
        patch.object(
            worker,
            "outbox_twilio_http_client",
            return_value=transport,
        ),
        patch.object(worker, "accept_whatsapp_outbound_attempt", return_value=True),
        patch.object(worker, "Client", return_value=MagicMock()) as client,
        patch(
            "routes.whatsapp_webhook._send_twilio_message",
            return_value=provider_message,
        ),
    ):
        result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=17)

    assert result.status == "accepted"
    client.assert_called_once_with(
        credentials.account_sid,
        credentials.auth_token,
        http_client=transport,
    )


def test_smtp_connection_receives_cron_timeout_without_changing_default_path():
    from services.email_service import _connect_smtp_server

    smtp = MagicMock()
    with patch("services.email_service.smtplib.SMTP", return_value=smtp) as smtp_ctor:
        _connect_smtp_server(
            host="smtp.example.test",
            port=587,
            use_tls=False,
            use_ssl=False,
            username="mailer",
            password="secret",
        )
    smtp_ctor.assert_called_once_with("smtp.example.test", 587)

    smtp.reset_mock()
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch("services.email_service.smtplib.SMTP", return_value=smtp) as smtp_ctor,
    ):
        _connect_smtp_server(
            host="smtp.example.test",
            port=587,
            use_tls=False,
            use_ssl=False,
            username="mailer",
            password="secret",
        )
    smtp_ctor.assert_called_once_with(
        "smtp.example.test",
        587,
        timeout=2.0,
    )


def test_socket_publish_uses_ephemeral_bounded_manager_only_in_cron():
    import socket_service

    app = Flask(__name__)
    app.config.update(
        SOCKETIO_MESSAGE_QUEUE_URL="redis://queue.example.test/0",
        SOCKETIO_MESSAGE_QUEUE_CHANNEL="chatboc-realtime-v1",
        SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS=2,
    )
    manager = MagicMock()
    with (
        app.app_context(),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(
            socket_service,
            "build_fail_closed_socketio_redis_manager",
            return_value=manager,
        ) as build_manager,
        patch.object(socket_service.socketio, "emit") as global_emit,
    ):
        socket_service._emit_with_outbox_budget(
            "survey.vote.created",
            {"event_id": "opaque"},
            room="encuesta:junin:prioridades",
        )

    global_emit.assert_not_called()
    build_manager.assert_called_once_with(
        "redis://queue.example.test/0",
        channel="chatboc-realtime-v1",
        timeout_seconds=1.0,
    )
    manager.emit.assert_called_once_with(
        "survey.vote.created",
        {"event_id": "opaque"},
        room="encuesta:junin:prioridades",
    )


def test_r2_client_uses_one_attempt_and_bounded_sockets_in_cron():
    from services.r2_service import R2Service

    service = R2Service()
    service.endpoint_url = "https://r2.example.test"
    service.access_key_id = "access"
    service.secret_access_key = "secret"
    service.bucket_name = "assets"

    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch("boto3.client", return_value=MagicMock()) as boto_client,
    ):
        service._get_client()

    config = boto_client.call_args.kwargs["config"]
    assert config.connect_timeout == 4.0
    assert config.read_timeout == 4.0
    assert config.retries["total_max_attempts"] == 1
    assert config.retries["mode"] == "standard"


def test_openai_bridge_clamps_timeout_and_keeps_sdk_retries_disabled():
    import services.openai_bridge as openai_bridge

    app = Flask(__name__)
    app.config.update(
        OPENAI_API_KEY="test-key",
        OPENAI_TIMEOUT_SECONDS=25,
        TESTING=False,
    )
    with (
        app.app_context(),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(openai_bridge, "_OPENAI_CLIENT", None),
        patch.object(openai_bridge, "_CLIENT_KEY_DIGEST", None),
        patch.object(openai_bridge, "httpx") as httpx_module,
        patch.object(openai_bridge, "OpenAI", return_value=MagicMock()) as constructor,
        patch.object(openai_bridge, "require_llm_provider_network"),
    ):
        openai_bridge._get_openai_client(app)

    httpx_module.Client.assert_called_once_with(
        proxy=None,
        trust_env=False,
        timeout=4.0,
    )
    constructor.assert_called_once_with(
        api_key="test-key",
        http_client=httpx_module.Client.return_value,
        max_retries=0,
        timeout=4.0,
    )


def test_openai_stt_overrides_a_warm_client_with_cron_timeout():
    import services.audio_transcription_service as audio_service

    warm_client = MagicMock()
    bounded_client = warm_client.with_options.return_value
    bounded_client.audio.transcriptions.create.return_value = SimpleNamespace(
        text="transcripcion"
    )
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(audio_service, "_get_openai_client", return_value=warm_client),
    ):
        result = audio_service._transcribe_with_openai(b"audio", "audio.ogg")

    assert result == "transcripcion"
    warm_client.with_options.assert_called_once_with(
        timeout=4.0,
        max_retries=0,
    )
    bounded_client.audio.transcriptions.create.assert_called_once()


def test_openai_maps_builds_bounded_no_retry_client_in_cron():
    import services.openai_maps_service as maps_service

    openai_module = MagicMock()
    httpx_module = MagicMock()
    http_client = httpx_module.Client.return_value.__enter__.return_value
    provider_client = openai_module.OpenAI.return_value
    provider_client.responses.create.return_value = SimpleNamespace(
        output_text='{"address": "Junin"}'
    )
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(maps_service, "openai", openai_module),
        patch.object(maps_service, "httpx", httpx_module),
        patch.object(maps_service, "llm_provider_network_allowed", return_value=True),
    ):
        result = maps_service._solicitar_json_a_openai(
            api_key="test-key",
            prompt="normalize",
            schema={"name": "map", "schema": {"type": "object"}},
            system_message="return json",
        )

    assert result == {"address": "Junin"}
    httpx_module.Client.assert_called_once_with(
        proxy=None,
        trust_env=False,
        timeout=4.0,
    )
    openai_module.OpenAI.assert_called_once_with(
        http_client=http_client,
        api_key="test-key",
        timeout=4.0,
        max_retries=0,
    )


@pytest.mark.parametrize(
    ("module_name", "timeout_environment"),
    [
        ("services.openai_text_service", {"OPENAI_TEXT_TIMEOUT_SECONDS": "45"}),
        ("services.vision_fallback_service", {"OPENAI_VISION_TIMEOUT_SECONDS": "30"}),
    ],
)
def test_specialized_openai_clients_use_cron_timeout(
    module_name,
    timeout_environment,
):
    import importlib

    module = importlib.import_module(module_name)
    app = Flask(__name__)
    app.config.update(OPENAI_API_KEY="test-key", TESTING=False)
    environment = {"OPENAI_API_KEY": "test-key", **timeout_environment}
    with (
        app.app_context(),
        patch.dict("os.environ", environment, clear=False),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(module, "_OPENAI_CLIENT", None),
        patch.object(module, "_OPENAI_CLIENT_KEY_DIGEST", None),
        patch.object(module, "httpx") as httpx_module,
        patch.object(module, "OpenAI", return_value=MagicMock()) as constructor,
    ):
        if hasattr(module, "llm_provider_network_allowed"):
            network_guard = patch.object(
                module,
                "llm_provider_network_allowed",
                return_value=True,
            )
        else:
            network_guard = patch.object(
                module,
                "_openai_network_allowed",
                return_value=True,
            )
        with network_guard:
            module._get_openai_client()

    constructor.assert_called_once_with(
        api_key="test-key",
        http_client=httpx_module.Client.return_value,
        max_retries=0,
        timeout=4.0,
    )


def test_cohere_stt_fallback_is_also_clamped_in_cron():
    import services.cohere_stt_bridge as cohere_stt

    httpx_module = MagicMock()
    response = httpx_module.Client.return_value.__enter__.return_value.post.return_value
    response.json.return_value = {"text": "audio"}
    with (
        patch.dict(
            "os.environ",
            {"COHERE_API_KEY": "test-key", "COHERE_STT_TIMEOUT": "60"},
            clear=False,
        ),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(cohere_stt, "httpx", httpx_module),
        patch.object(cohere_stt, "llm_provider_network_allowed", return_value=True),
    ):
        assert cohere_stt.transcribir_audio_cohere(b"audio", "audio/ogg") == "audio"

    httpx_module.Client.assert_called_once_with(timeout=4.0)


def test_openai_tts_is_bounded_and_disables_sdk_retries_only_in_cron():
    import services.openai_tts_bridge as openai_tts

    httpx_module = MagicMock()
    openai_module = MagicMock()
    response = openai_module.OpenAI.return_value.audio.speech.create.return_value
    response.stream_to_file.return_value = None
    with (
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(openai_tts, "httpx", httpx_module),
        patch.object(openai_tts, "openai", openai_module),
        patch.object(openai_tts.os, "makedirs"),
    ):
        openai_tts.clear_tts_cache()
        assert openai_tts.generar_audio_openai("cron tts bounded") is not None

    httpx_module.Client.assert_called_once_with(
        proxy=None,
        trust_env=False,
        timeout=4.0,
    )
    openai_module.OpenAI.assert_called_once_with(
        api_key="test-key",
        timeout=4.0,
        max_retries=0,
        http_client=httpx_module.Client.return_value,
    )

    httpx_module.reset_mock()
    openai_module.reset_mock()
    with (
        patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
        patch.object(openai_tts, "httpx", httpx_module),
        patch.object(openai_tts, "openai", openai_module),
        patch.object(openai_tts.os, "makedirs"),
    ):
        openai_tts.clear_tts_cache()
        assert openai_tts.generar_audio_openai("normal tts unchanged") is not None

    httpx_module.Client.assert_called_once_with(proxy=None, trust_env=False)
    openai_module.OpenAI.assert_called_once_with(
        api_key="test-key",
        http_client=httpx_module.Client.return_value,
    )


def test_cohere_tts_is_bounded_with_an_explicit_no_retry_transport_in_cron():
    import services.cohere_tts_bridge as cohere_tts

    httpx_module = MagicMock()
    response = httpx_module.Client.return_value.__enter__.return_value.post.return_value
    response.headers = {"Content-Type": "audio/mpeg"}
    response.content = b"audio"
    transport = httpx_module.HTTPTransport.return_value
    with (
        patch.dict(
            "os.environ",
            {"COHERE_API_KEY": "test-key", "COHERE_TTS_TIMEOUT": "60"},
            clear=False,
        ),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(cohere_tts, "httpx", httpx_module),
        patch.object(cohere_tts.os, "makedirs"),
        patch.object(cohere_tts, "open", MagicMock()),
    ):
        assert cohere_tts.generar_audio_cohere("cron cohere tts") is not None

    httpx_module.HTTPTransport.assert_called_once_with(retries=0)
    httpx_module.Client.assert_called_once_with(timeout=4.0, transport=transport)


def test_cohere_vision_clamps_timeout_and_suppresses_sdk_method_retry_in_cron():
    import services.vision_fallback_service as vision

    cohere_module = MagicMock()
    client = cohere_module.Client.return_value
    client.chat.side_effect = TypeError("images unsupported")
    with (
        patch.dict(sys.modules, {"cohere": cohere_module}),
        patch.dict(
            "os.environ",
            {"COHERE_API_KEY": "test-key", "VISION_COHERE_ENABLED": "true"},
            clear=False,
        ),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(vision, "llm_provider_network_allowed", return_value=True),
    ):
        assert vision._call_cohere(b"image") is None

    cohere_module.Client.assert_called_once_with("test-key", timeout=4.0)
    client.chat.assert_called_once()
    client.generate.assert_not_called()


def test_huggingface_vision_client_clamps_only_inside_cron():
    import services.huggingface_inference_service as huggingface

    hub_module = MagicMock()
    with (
        patch.dict(sys.modules, {"huggingface_hub": hub_module}),
        patch.dict(
            "os.environ",
            {
                "HUGGINGFACE_API_TOKEN": "test-token",
                "HUGGINGFACE_TIMEOUT_SECONDS": "60",
            },
            clear=False,
        ),
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
    ):
        huggingface._get_client(model="test/vision")

    hub_module.InferenceClient.assert_called_once_with(
        model="test/vision",
        provider="auto",
        token="test-token",
        timeout=4.0,
    )

    hub_module.InferenceClient.reset_mock()
    with (
        patch.dict(sys.modules, {"huggingface_hub": hub_module}),
        patch.dict(
            "os.environ",
            {
                "HUGGINGFACE_API_TOKEN": "test-token",
                "HUGGINGFACE_TIMEOUT_SECONDS": "60",
            },
            clear=False,
        ),
    ):
        huggingface._get_client(model="test/vision")

    hub_module.InferenceClient.assert_called_once_with(
        model="test/vision",
        provider="auto",
        token="test-token",
        timeout=60.0,
    )


def test_tts_generation_lock_wait_is_bounded_in_cron():
    import services.tts_orchestrator as tts

    lock = MagicMock()
    lock.acquire.return_value = False
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        pytest.raises(
            OutboxExecutionBudgetExceeded,
            match="outbox_tts_generation_lock_timeout",
        ),
    ):
        with tts._bounded_generation_lock(lock):
            raise AssertionError("unreachable")

    lock.acquire.assert_called_once_with(timeout=4.0)
    lock.release.assert_not_called()


def test_twilio_media_download_clamps_requests_and_checks_stream_deadline():
    from routes import whatsapp_webhook

    response = MagicMock()
    response.iter_content.return_value = [b"media"]
    response.headers = {}
    with (
        activate_outbox_execution_budget(
            deadline_monotonic=145.0,
            clock=lambda: 100.0,
        ),
        patch.object(whatsapp_webhook, "require_provider_network"),
        patch.object(
            whatsapp_webhook.requests,
            "get",
            return_value=response,
        ) as request_get,
    ):
        payload = whatsapp_webhook._download_twilio_media(
            "https://media.example.test/object",
            auth=("account", "token"),
            timeout=12,
            max_bytes=1024,
        )

    assert payload == b"media"
    request_get.assert_called_once_with(
        "https://media.example.test/object",
        auth=("account", "token"),
        stream=True,
        timeout=4.0,
    )
    response.close.assert_called_once_with()
