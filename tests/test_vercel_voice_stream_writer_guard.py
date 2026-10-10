"""Offline HTTP upgrade admission tests; no live voice/provider acceptance."""
from contextlib import contextmanager
from unittest.mock import MagicMock

from flask import Flask
import pytest

from config import Config, _env_strict_opt_in
from cutover_writer_fence import is_cutover_writer_view
from middleware.cutover_writer_fence import register_cutover_writer_fence
from routes.voice_routes import voice_bp
from services.global_writer_authority import (
    GlobalWriterAuthorityDecision,
    GlobalWriterAuthorityLease,
)


@pytest.fixture
def voice_runtime(monkeypatch):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        ENV="prod",
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
        CUTOVER_WRITER_FENCE_ENABLED=False,
        ENABLE_VOICE_CONSENT_LIFECYCLE_V1=False,
    )
    authority = MagicMock(return_value=GlobalWriterAuthorityDecision(
        allowed=True, enabled=True, reason_code="global_writer_authority_allowed", epoch=1,
    ))
    lease_events = []

    @contextmanager
    def lease(config, *, request_lifetime=False):
        lease_events.append(("acquired", request_lifetime))
        try:
            yield GlobalWriterAuthorityLease(decision=authority.return_value, executor=None)
        finally:
            lease_events.append(("released", request_lifetime))

    monkeypatch.setattr("middleware.cutover_writer_fence.evaluate_global_writer_authority", authority)
    monkeypatch.setattr("middleware.cutover_writer_fence.global_writer_authority_lease", lease)
    register_cutover_writer_fence(app)
    app.register_blueprint(voice_bp)
    socket = MagicMock(mode="werkzeug")
    accept = MagicMock(return_value=socket)
    service = MagicMock()
    provider = MagicMock(side_effect=AssertionError("provider must not be reached"))
    twilio_provider = MagicMock(side_effect=AssertionError("Twilio must not be reached"))
    monkeypatch.setattr("flask_sock.Server", accept)
    monkeypatch.setattr("routes.voice_routes.VoiceStreamService", service)
    monkeypatch.setattr("services.voice_stream_service.ws_connect", provider)
    monkeypatch.setattr("services.voice_stream_service.TwilioClient", twilio_provider)
    return app, accept, service, provider, twilio_provider, authority, lease_events


def _upgrade(app):
    return app.test_client().get(
        "/twilio/voice/stream",
        headers={"Connection": "Upgrade", "Upgrade": "websocket"},
    )


def _assert_no_accept_or_service(runtime):
    _, accept, service, provider, twilio_provider, *_ = runtime
    accept.assert_not_called()
    service.assert_not_called()
    provider.assert_not_called()
    twilio_provider.assert_not_called()


def test_voice_opt_in_config_is_disabled_by_default():
    assert Config.VERCEL_VOICE_STREAM_WRITER_ENABLED is False


@pytest.mark.parametrize("raw", [None, "", "false", "0", "tru", "enabled"])
def test_voice_config_opt_in_missing_or_invalid_remains_disabled(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("VERCEL_VOICE_STREAM_WRITER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("VERCEL_VOICE_STREAM_WRITER_ENABLED", raw)
    assert _env_strict_opt_in("VERCEL_VOICE_STREAM_WRITER_ENABLED") is False


@pytest.mark.parametrize("consent_enabled", [False, True])
@pytest.mark.parametrize("flag", [None, False, "false", "tru", "enabled"])
def test_vercel_guard_refuses_actual_upgrade_before_accept_service_and_providers(
    voice_runtime, consent_enabled, flag,
):
    app, *_, lease_events = voice_runtime
    app.config["ENABLE_VOICE_CONSENT_LIFECYCLE_V1"] = consent_enabled
    if flag is not None:
        app.config["VERCEL_VOICE_STREAM_WRITER_ENABLED"] = flag
    response = _upgrade(app)
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "vercel_voice_stream_writer_disabled"
    assert response.get_json()["request_dispatched"] is False
    assert response.headers["Cache-Control"] == "no-store"
    _assert_no_accept_or_service(voice_runtime)
    assert lease_events == [("acquired", True), ("released", True)]


def test_registered_flask_sock_view_preserves_writer_marker(voice_runtime):
    app = voice_runtime[0]
    view = app.view_functions["voice.voice_stream_socket"]
    assert is_cutover_writer_view(view)
    assert any(rule.rule == "/twilio/voice/stream" and rule.websocket
               for rule in app.url_map.iter_rules("voice.voice_stream_socket"))


def test_explicit_runtime_disable_wins_over_environment_opt_in(voice_runtime, monkeypatch):
    app = voice_runtime[0]
    app.config["VERCEL_VOICE_STREAM_WRITER_ENABLED"] = False
    monkeypatch.setenv("VERCEL_VOICE_STREAM_WRITER_ENABLED", "true")
    response = _upgrade(app)
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "vercel_voice_stream_writer_disabled"
    _assert_no_accept_or_service(voice_runtime)


def test_native_vercel_production_signal_wins_over_stale_testing_env(voice_runtime, monkeypatch):
    app = voice_runtime[0]
    app.config["ENV"] = "testing"
    monkeypatch.setenv("VERCEL_ENV", "production")
    response = _upgrade(app)
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "vercel_voice_stream_writer_disabled"
    _assert_no_accept_or_service(voice_runtime)


def test_enabled_stream_cannot_bypass_static_writer_fence(voice_runtime):
    app = voice_runtime[0]
    app.config.update(VERCEL_VOICE_STREAM_WRITER_ENABLED=True, CUTOVER_WRITER_FENCE_ENABLED=True)
    response = _upgrade(app)
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "cutover_writer_fence_enabled"
    _assert_no_accept_or_service(voice_runtime)


def test_enabled_stream_cannot_bypass_global_writer_authority(voice_runtime):
    app, _, _, _, _, authority, lease_events = voice_runtime
    app.config["VERCEL_VOICE_STREAM_WRITER_ENABLED"] = True
    authority.return_value = GlobalWriterAuthorityDecision(
        allowed=False, enabled=True, reason_code="global_writer_authority_vercel_fenced",
    )
    response = _upgrade(app)
    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "global_writer_authority_vercel_fenced"
    _assert_no_accept_or_service(voice_runtime)
    assert lease_events == []


@pytest.mark.parametrize("flag", [True, "true", "1", "yes", "on"])
def test_explicit_opt_in_permits_wrapped_handler_under_global_lease(voice_runtime, flag):
    app, accept, service, provider, twilio_provider, authority, lease_events = voice_runtime
    app.config["VERCEL_VOICE_STREAM_WRITER_ENABLED"] = flag
    response = _upgrade(app)
    assert response.status_code == 200
    authority.assert_called_once()
    accept.assert_called_once()
    service.assert_called_once_with(accept.return_value, app=app)
    service.return_value.run.assert_called_once_with()
    provider.assert_not_called()
    twilio_provider.assert_not_called()
    assert lease_events == [("acquired", True), ("released", True)]


@pytest.mark.parametrize("overrides", [
    {"CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED": False},
    {"CUTOVER_RUNTIME_IDENTITY": "render"},
    {"ENV": "testing"},
])
def test_non_guarded_legacy_modes_preserve_transport_behavior(voice_runtime, overrides):
    app, accept, service, *_ = voice_runtime
    app.config.update(overrides)
    response = _upgrade(app)
    assert response.status_code == 200
    accept.assert_called_once()
    service.return_value.run.assert_called_once_with()
