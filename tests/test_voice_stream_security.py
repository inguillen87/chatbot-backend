import json
import time
import unittest
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app import create_app
from config import TestConfig
from extensions import db
from models import ProviderSender, TenantProfile, User
from routes.voice_routes import voice_bp
from services.voice_stream_envelope import (
    VoiceStreamEnvelopeError,
    consume_voice_stream_envelope_once,
    create_voice_stream_envelope,
    resolve_voice_stream_signing_key,
    verify_voice_stream_envelope,
)
from services.voice_stream_service import VoiceStreamService


SIGNING_SECRET = "voice-stream-test-secret-that-is-longer-than-32-bytes"


class _InboundSocket:
    def __init__(self, events):
        self._events = [json.dumps(event) for event in events]
        self.closed = False

    def receive(self, timeout=None):
        if not self._events:
            return None
        return self._events.pop(0)

    def close(self):
        self.closed = True


class _NoTimeoutSocket:
    def __init__(self):
        self.closed = False

    def receive(self):
        raise AssertionError("unbounded receive must never be called")

    def close(self):
        self.closed = True


def _start_event(custom, *, call_sid="CA-secure-123"):
    return {
        "event": "start",
        "start": {
            "streamSid": "MZ-secure-123",
            "callSid": call_sid,
            "customParameters": dict(custom),
        },
    }


def _signed_parameters(
    config,
    *,
    now=None,
    tenant_slug="",
    to_number="+17432643718",
):
    return create_voice_stream_envelope(
        call_sid="CA-secure-123",
        from_number="+5492613168608",
        to_number=to_number,
        tenant_slug=tenant_slug,
        vertical="municipio",
        intent="reclamos",
        chat_session_id="voice-session-123",
        demo=False,
        config=config,
        now=now,
        nonce=f"nonce_{uuid.uuid4().hex}",
    )


class VoiceStreamEnvelopeTests(unittest.TestCase):
    def test_twilio_fallback_key_is_domain_separated(self):
        token = "twilio-auth-token-that-is-long-enough"
        key = resolve_voice_stream_signing_key({"TWILIO_AUTH_TOKEN": token})

        self.assertIsNotNone(key)
        self.assertNotEqual(key, token.encode("utf-8"))
        self.assertEqual(len(key), 32)

    def test_tampering_and_transport_mismatch_are_rejected(self):
        config = {"VOICE_STREAM_SIGNING_SECRET": SIGNING_SECRET}
        params = _signed_parameters(config)

        tampered = dict(params)
        tampered["tenant_slug"] = "another-tenant"
        with self.assertRaisesRegex(VoiceStreamEnvelopeError, "signature_mismatch"):
            verify_voice_stream_envelope(
                tampered,
                start=_start_event(tampered)["start"],
                config=config,
            )

        with self.assertRaisesRegex(VoiceStreamEnvelopeError, "inconsistent_call_sid"):
            verify_voice_stream_envelope(
                params,
                start=_start_event(params, call_sid="CA-other-call")["start"],
                config=config,
            )

    def test_test_store_consumes_an_envelope_only_once(self):
        config = {
            "TESTING": True,
            "VOICE_STREAM_SIGNING_SECRET": SIGNING_SECRET,
            "VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE": True,
        }
        params = _signed_parameters(config)
        verified = verify_voice_stream_envelope(
            params,
            start=_start_event(params)["start"],
            config=config,
        )

        consume_voice_stream_envelope_once(verified, config=config)
        with self.assertRaisesRegex(VoiceStreamEnvelopeError, "envelope_replayed"):
            consume_voice_stream_envelope_once(verified, config=config)

    def test_runtime_without_shared_replay_store_fails_closed(self):
        config = {"VOICE_STREAM_SIGNING_SECRET": SIGNING_SECRET}
        params = _signed_parameters(config)
        verified = verify_voice_stream_envelope(
            params,
            start=_start_event(params)["start"],
            config=config,
        )

        with patch("services.voice_stream_envelope._replay_redis_url", return_value=None):
            with self.assertRaisesRegex(VoiceStreamEnvelopeError, "replay_store_missing"):
                consume_voice_stream_envelope_once(verified, config=config)


class VoiceStreamDeploymentConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[1]

    def test_env_example_declares_fail_closed_voice_stream_contract(self):
        example = (self.repo_root / ".env.example").read_text(encoding="utf-8")

        self.assertIn("VOICE_STREAM_SIGNING_SECRET=\n", example)
        self.assertIn("VOICE_STREAM_REPLAY_REDIS_URL=\n", example)
        self.assertIn("VOICE_STREAM_ENVELOPE_TTL_SECONDS=120", example)
        self.assertIn("VOICE_STREAM_PREFLIGHT_TIMEOUT_SECONDS=5", example)
        self.assertIn("VOICE_STREAM_PREFLIGHT_MAX_EVENTS=3", example)
        self.assertIn("VOICE_STREAM_PREFLIGHT_MAX_MESSAGE_BYTES=32768", example)
        self.assertIn("VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE=false", example)
        self.assertNotIn(SIGNING_SECRET, example)

    def test_render_declares_secret_without_embedding_it_and_reuses_shared_redis(self):
        render = (self.repo_root / "render.yaml").read_text(encoding="utf-8")

        self.assertRegex(
            render,
            r"- key: VOICE_STREAM_SIGNING_SECRET\s+sync: false",
        )
        self.assertNotRegex(
            render,
            r"- key: VOICE_STREAM_SIGNING_SECRET\s+value:",
        )
        self.assertIn("- key: SOCKETIO_MESSAGE_QUEUE_URL", render)
        self.assertIn("One-time replay consumption reuses SOCKETIO_MESSAGE_QUEUE_URL", render)
        for key, value in (
            ("VOICE_STREAM_ENVELOPE_TTL_SECONDS", "120"),
            ("VOICE_STREAM_PREFLIGHT_TIMEOUT_SECONDS", "5"),
            ("VOICE_STREAM_PREFLIGHT_MAX_EVENTS", "3"),
            ("VOICE_STREAM_PREFLIGHT_MAX_MESSAGE_BYTES", "32768"),
        ):
            self.assertRegex(
                render,
                rf'- key: {key}\s+value: "{value}"',
            )
        self.assertNotIn("redis://shared.example.test", render)
        self.assertNotIn(SIGNING_SECRET, render)

    def test_runbook_states_exact_shared_store_guarantee(self):
        runbook = (
            self.repo_root / "docs" / "BACKEND_TO_FRONTEND_SYNC_REALTIME_VOICE_2026-05-07.md"
        ).read_text(encoding="utf-8")

        self.assertIn("Redis `SET NX EX`", runbook)
        self.assertIn("store ausente o caido rechaza el stream", runbook)
        self.assertIn("El store en memoria solo puede habilitarse", runbook)

    def test_shared_store_uses_atomic_nx_consumption(self):
        class FakeRedis:
            def __init__(self):
                self.keys = set()

            def set(self, key, value, *, nx, ex):
                self.assertions = (value, nx, ex)
                if key in self.keys:
                    return False
                self.keys.add(key)
                return True

        config = {
            "VOICE_STREAM_SIGNING_SECRET": SIGNING_SECRET,
            "VOICE_STREAM_REPLAY_REDIS_URL": "redis://shared.example.test:6379/0",
        }
        params = _signed_parameters(config)
        verified = verify_voice_stream_envelope(
            params,
            start=_start_event(params)["start"],
            config=config,
        )
        fake_redis = FakeRedis()

        with patch("services.voice_stream_envelope._redis_client", return_value=fake_redis):
            consume_voice_stream_envelope_once(verified, config=config)
            with self.assertRaisesRegex(VoiceStreamEnvelopeError, "envelope_replayed"):
                consume_voice_stream_envelope_once(verified, config=config)
        self.assertTrue(fake_redis.assertions[1])
        self.assertGreater(fake_redis.assertions[2], 0)

    def test_shared_store_failure_is_fail_closed(self):
        class UnavailableRedis:
            def set(self, *_args, **_kwargs):
                raise ConnectionError("redis unavailable")

        config = {
            "VOICE_STREAM_SIGNING_SECRET": SIGNING_SECRET,
            "VOICE_STREAM_REPLAY_REDIS_URL": "redis://shared.example.test:6379/0",
        }
        params = _signed_parameters(config)
        verified = verify_voice_stream_envelope(
            params,
            start=_start_event(params)["start"],
            config=config,
        )

        with patch(
            "services.voice_stream_envelope._redis_client",
            return_value=UnavailableRedis(),
        ):
            with self.assertRaisesRegex(VoiceStreamEnvelopeError, "replay_store_unavailable"):
                consume_voice_stream_envelope_once(verified, config=config)


class VoiceStreamTwimlSecurityTests(unittest.TestCase):
    def _app(self, **overrides):
        app = Flask(f"voice-stream-security-{len(overrides)}")
        app.config.update(
            TESTING=True,
            BACKEND_URL="https://api.chatboc.test",
            VOICE_STREAM_SIGNING_SECRET=SIGNING_SECRET,
            ENABLE_VOICE_CONSENT_LIFECYCLE_V1=True,
            **overrides,
        )
        app.register_blueprint(voice_bp)
        return app

    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_twiml_contains_verifiable_envelope_without_secret_leakage(self):
        app = self._app()
        tenant = SimpleNamespace(
            id=7,
            slug="junin-1",
            configuracion={
                "voice_consent_policy": {
                    "version": "voice.consent.v1",
                    "ai_processing": "explicit_per_call",
                    "recording": "disabled",
                }
            },
            is_active=True,
        )
        lifecycle = SimpleNamespace(
            consent_status="granted",
            consent_policy_version="voice.consent.v1",
            state="stream_authorized",
            ai_processing_allowed=True,
            recording_allowed=False,
            recording_enabled=False,
        )
        with patch(
            "routes.voice_routes.resolve_authoritative_voice_tenant", return_value=tenant
        ), patch(
            "routes.voice_routes.begin_voice_consent", return_value=lifecycle
        ), patch(
            "routes.voice_routes.assert_voice_stream_authorized", return_value=lifecycle
        ):
            response = app.test_client().post(
                "/twilio/voice/inbound?tenant=junin&vertical=municipio&intent=reclamos&chat_session_id=session-7",
                data={
                    "CallSid": "CA-secure-123",
                    "From": "+5492613168608",
                    "To": "+17432643718",
                    "Direction": "inbound",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("<Connect>", body)
        self.assertNotIn(SIGNING_SECRET, body)
        root = ET.fromstring(body)
        params = {
            node.attrib["name"]: node.attrib["value"]
            for node in root.findall(".//Parameter")
        }
        self.assertIn("signature", params)
        self.assertIn("ts", params)
        self.assertIn("nonce", params)
        self.assertEqual(params["tenant_slug"], "junin-1")
        verified = verify_voice_stream_envelope(
            params,
            start={"streamSid": "MZ-route-test", "callSid": "CA-secure-123"},
            config=app.config,
        )
        self.assertEqual(verified["chat_session_id"], "session-7")
        self.assertEqual(verified["intent"], "reclamos")

    @patch.dict("os.environ", {}, clear=True)
    @patch("routes.voice_routes.TWILIO_AUTH_TOKEN", None)
    def test_missing_signing_material_falls_back_without_stream(self):
        app = Flask("voice-stream-no-secret")
        app.config.update(
            TESTING=True,
            BACKEND_URL="https://api.chatboc.test",
            ENABLE_VOICE_CONSENT_LIFECYCLE_V1=True,
        )
        app.register_blueprint(voice_bp)

        tenant = SimpleNamespace(
            id=7,
            slug="junin-1",
            configuracion={
                "voice_consent_policy": {
                    "version": "voice.consent.v1",
                    "ai_processing": "explicit_per_call",
                    "recording": "disabled",
                }
            },
            is_active=True,
        )
        lifecycle = SimpleNamespace(
            consent_status="granted",
            consent_policy_version="voice.consent.v1",
            state="stream_authorized",
            ai_processing_allowed=True,
            recording_allowed=False,
            recording_enabled=False,
        )
        with patch(
            "routes.voice_routes.resolve_authoritative_voice_tenant", return_value=tenant
        ), patch(
            "routes.voice_routes.begin_voice_consent", return_value=lifecycle
        ), patch(
            "routes.voice_routes.assert_voice_stream_authorized", return_value=lifecycle
        ):
            response = app.test_client().post(
                "/twilio/voice/inbound",
                data={
                    "CallSid": "CA-secure-123",
                    "From": "+5492613168608",
                    "To": "+17432643718",
                    "Direction": "inbound",
                },
            )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<Connect>", body)
        self.assertNotIn("/twilio/voice/stream", body)
        self.assertIn("<Redirect>", body)


class VoiceStreamPreflightTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask("voice-stream-preflight")
        self.app.config.update(
            TESTING=True,
            OPENAI_API_KEY="test-openai-key",
            VOICE_STREAM_SIGNING_SECRET=SIGNING_SECRET,
            VOICE_STREAM_ENVELOPE_TTL_SECONDS=30,
            VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE=True,
            ENABLE_VOICE_CONSENT_LIFECYCLE_V1=True,
        )

    def _service(self, params):
        socket = _InboundSocket(
            [
                {"event": "connected", "protocol": "Call", "version": "1.0.0"},
                _start_event(params),
            ]
        )
        return VoiceStreamService(socket, app=self.app), socket

    @patch("services.voice_stream_service.ws_connect")
    def test_invalid_signature_never_opens_openai(self, mock_connect):
        params = _signed_parameters(self.app.config)
        params["signature"] = "0" * 64
        service, socket = self._service(params)

        with patch.object(service, "_resolve_context") as resolve_context:
            service.run()

        mock_connect.assert_not_called()
        resolve_context.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_service.ws_connect")
    def test_expired_envelope_never_opens_openai(self, mock_connect):
        params = _signed_parameters(self.app.config, now=int(time.time()) - 120)
        service, socket = self._service(params)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_service.ws_connect")
    def test_inconsistent_call_sid_never_opens_openai(self, mock_connect):
        params = _signed_parameters(self.app.config)
        socket = _InboundSocket([_start_event(params, call_sid="CA-tampered")])
        service = VoiceStreamService(socket, app=self.app)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_service.ws_connect")
    def test_preflight_event_limit_never_opens_openai(self, mock_connect):
        connected = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
        socket = _InboundSocket([connected, connected, connected, connected])
        service = VoiceStreamService(socket, app=self.app)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_service.ws_connect")
    def test_socket_without_timeout_receive_fails_closed(self, mock_connect):
        socket = _NoTimeoutSocket()
        service = VoiceStreamService(socket, app=self.app)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_envelope._replay_redis_url", return_value=None)
    @patch("services.voice_stream_service.ws_connect")
    def test_valid_envelope_without_shared_runtime_store_fails_closed(
        self,
        mock_connect,
        _mock_replay_url,
    ):
        app = Flask("voice-stream-no-replay-store")
        app.config.update(
            TESTING=False,
            OPENAI_API_KEY="test-openai-key",
            VOICE_STREAM_SIGNING_SECRET=SIGNING_SECRET,
        )
        params = _signed_parameters(app.config)
        socket = _InboundSocket([_start_event(params)])
        service = VoiceStreamService(socket, app=app)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)

    @patch("services.voice_stream_service.ws_connect", side_effect=RuntimeError("handshake reached"))
    def test_valid_start_reaches_openai_handshake_after_tenant_resolution(self, mock_connect):
        params = _signed_parameters(self.app.config)
        service, socket = self._service(params)
        service._consent_authorized = True
        service._consent_lifecycle_tenant_id = 1
        service.tenant_profile = SimpleNamespace(id=1)

        with patch.object(
            service, "_authorize_durable_voice_consent", return_value=True
        ), patch.object(service, "_resolve_context", return_value=True) as resolve_context:
            service.run()

        resolve_context.assert_called_once_with(
            "+5492613168608",
            "+17432643718",
            "CA-secure-123",
        )
        mock_connect.assert_called_once()
        self.assertFalse(socket.closed)

    @patch("services.voice_stream_service.ws_connect", side_effect=RuntimeError("handshake reached"))
    def test_same_envelope_cannot_open_a_second_handshake(self, mock_connect):
        params = _signed_parameters(self.app.config)
        first, _ = self._service(params)
        second, second_socket = self._service(params)
        for service in (first, second):
            service._consent_authorized = True
            service._consent_lifecycle_tenant_id = 1
            service.tenant_profile = SimpleNamespace(id=1)

        with patch.object(
            first, "_authorize_durable_voice_consent", return_value=True
        ), patch.object(first, "_resolve_context", return_value=True):
            first.run()
        with patch.object(
            second, "_authorize_durable_voice_consent", return_value=True
        ), patch.object(second, "_resolve_context") as second_resolve:
            second.run()

        mock_connect.assert_called_once()
        second_resolve.assert_not_called()
        self.assertTrue(second_socket.closed)


class VoiceStreamTenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config.update(
            OPENAI_API_KEY="test-openai-key",
            VOICE_STREAM_SIGNING_SECRET=SIGNING_SECRET,
            VOICE_STREAM_REPLAY_ALLOW_IN_MEMORY_TEST_STORE=True,
        )
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @staticmethod
    def _tenant(slug):
        owner = User(
            email=f"{slug}@example.com",
            name=f"Owner {slug}",
            password_hash="test",
            rol="admin_pyme",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug,
            nombre=f"Tenant {slug}",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        return owner, tenant

    def test_requested_tenant_cannot_replace_sender_tenant(self):
        owner_a, tenant_a = self._tenant("voice-sender-a")
        _, tenant_b = self._tenant("voice-requested-b")
        db.session.add(
            ProviderSender(
                tenant_id=tenant_a.id,
                channel="voice",
                phone_number="+15551234567",
                sender_id="+15551234567",
                status="active",
            )
        )
        db.session.commit()

        service = VoiceStreamService(_InboundSocket([]), app=self.app)
        service.requested_tenant_slug = tenant_b.slug
        service.from_number = "+5492613168608"
        service.to_number = "+15551234567"

        resolved = service._resolve_context(
            service.from_number,
            service.to_number,
            "CA-tenant-mismatch",
        )

        self.assertFalse(resolved)
        self.assertEqual(service.tenant_profile.id, tenant_a.id)
        self.assertEqual(service.owner_user.id, owner_a.id)

    def test_demo_marker_alone_does_not_bypass_sender_tenant(self):
        owner_a, tenant_a = self._tenant("voice-sender-demo-guard")
        _, tenant_b = self._tenant("chatboc-platform")
        db.session.add(
            ProviderSender(
                tenant_id=tenant_a.id,
                channel="voice",
                phone_number="+15557654321",
                sender_id="+15557654321",
                status="active",
            )
        )
        db.session.commit()

        service = VoiceStreamService(_InboundSocket([]), app=self.app)
        service.demo_hub = "chatboc"
        service.requested_tenant_slug = tenant_b.slug
        service.from_number = "+5492613168608"
        service.to_number = "+15557654321"

        resolved = service._resolve_context(
            service.from_number,
            service.to_number,
            "CA-demo-marker-mismatch",
        )

        self.assertFalse(resolved)
        self.assertEqual(service.tenant_profile.id, tenant_a.id)
        self.assertEqual(service.owner_user.id, owner_a.id)

    @patch("services.voice_stream_service.ws_connect")
    def test_tenant_mismatch_stops_before_openai_handshake(self, mock_connect):
        _, tenant_a = self._tenant("voice-preflight-sender")
        _, tenant_b = self._tenant("voice-preflight-requested")
        sender_number = "+15559876543"
        db.session.add(
            ProviderSender(
                tenant_id=tenant_a.id,
                channel="voice",
                phone_number=sender_number,
                sender_id=sender_number,
                status="active",
            )
        )
        db.session.commit()
        params = _signed_parameters(
            self.app.config,
            tenant_slug=tenant_b.slug,
            to_number=sender_number,
        )
        socket = _InboundSocket([_start_event(params)])
        service = VoiceStreamService(socket, app=self.app)

        service.run()

        mock_connect.assert_not_called()
        self.assertTrue(socket.closed)


if __name__ == "__main__":
    unittest.main()
