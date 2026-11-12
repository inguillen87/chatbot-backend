"""Optional welcome audio must not remove the text menu on provider failure."""
import os
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SKIP_INIT_TENANTS", "1")

from flask import Flask
from routes import whatsapp_webhook as webhook
from services.outbox_execution_budget import OutboxExecutionBudgetExceeded
from tests import test_whatsapp_inbound_queue_webhook as signed_fixture
from extensions import db
from models import ChatSessionContext


def menu_payload():
    return {
        "message_body": "Opciones municipales del fixture",
        "options_list": [{"texto": "Reclamos", "action_id": "mostrar_menu_reclamos"}],
        "message_type": "text",
        "audio_text": "Opciones municipales del fixture",
        "generar_audio": True,
        "audio_cache_policy": {"kind": "fixed_menu"},
        "tts_cache_namespace": "whatsapp:menu:fixture:main-menu:whatsapp:full:v2",
    }


class WelcomeOptionalAudioUnitTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)

    def test_ordinary_exception_preserves_menu_and_skips_retry_without_audio(self):
        payload = menu_payload()
        with patch.object(webhook, "generar_audio", side_effect=RuntimeError("private provider detail")) as tts:
            with self.assertLogs(self.app.logger, level="WARNING") as captured:
                webhook._ensure_welcome_audio_payload(payload)
            webhook._ensure_welcome_audio_payload(payload)
        self.assertEqual(tts.call_count, 1)
        self.assertTrue(payload["skip_audio_generation"])
        self.assertNotIn("audio_url", payload)
        self.assertEqual(payload["message_body"], menu_payload()["message_body"])
        self.assertEqual(payload["options_list"], menu_payload()["options_list"])
        self.assertIn("RuntimeError", "\n".join(captured.output))
        self.assertNotIn("private provider detail", "\n".join(captured.output))

    def test_none_audio_result_is_not_retried_without_fallback(self):
        payload = menu_payload()
        with patch.object(webhook, "generar_audio", return_value=None) as tts:
            webhook._ensure_welcome_audio_payload(payload)
            webhook._ensure_welcome_audio_payload(payload)
        self.assertEqual(tts.call_count, 1)
        self.assertTrue(payload["skip_audio_generation"])
        self.assertNotIn("audio_url", payload)

    def test_fixed_menu_fallback_is_preserved_after_exception_or_none(self):
        for provider_outcome in (RuntimeError("local provider failure"), None):
            with self.subTest(provider_outcome=type(provider_outcome).__name__):
                payload = menu_payload()
                payload["audio_url"] = "https://fixture.invalid/cached-menu.mp3"
                args = {"side_effect": provider_outcome} if isinstance(provider_outcome, Exception) else {"return_value": None}
                with patch.object(webhook, "generar_audio", **args) as tts:
                    webhook._ensure_welcome_audio_payload(payload)
                    webhook._ensure_welcome_audio_payload(payload)
                self.assertEqual(tts.call_count, 1)
                self.assertEqual(payload["audio_url"], "https://fixture.invalid/cached-menu.mp3")
                self.assertTrue(payload["skip_audio_generation"])

    def test_skip_guard_keeps_existing_audio_url_before_fixed_menu_pop(self):
        payload = menu_payload()
        payload.update(skip_audio_generation=True, audio_url="https://fixture.invalid/already-selected.mp3")
        with patch.object(webhook, "generar_audio") as tts:
            webhook._ensure_welcome_audio_payload(payload)
        tts.assert_not_called()
        self.assertEqual(payload["audio_url"], "https://fixture.invalid/already-selected.mp3")

    def test_success_preserves_tenant_namespace_and_replaces_fallback(self):
        payload = menu_payload()
        payload.update(audio_url="https://fixture.invalid/old.mp3", tts_cache_text="Stable tenant menu", tts_voice="shimmer", tts_model="fixture-model", tts_speed=0.92)
        with patch.object(webhook, "generar_audio", return_value="https://fixture.invalid/new.mp3") as tts:
            webhook._ensure_welcome_audio_payload(payload)
        self.assertEqual(payload["audio_url"], "https://fixture.invalid/new.mp3")
        self.assertFalse(payload.get("skip_audio_generation", False))
        tts.assert_called_once_with("Stable tenant menu", voice="shimmer", model="fixture-model", style=None, speed=0.92, cache_namespace="whatsapp:menu:fixture:main-menu:whatsapp:full:v2")

    def test_execution_budget_control_propagates_without_optional_failure_marker(self):
        payload = menu_payload()
        with patch.object(webhook, "generar_audio", side_effect=OutboxExecutionBudgetExceeded("outbox_execution_budget_exhausted")):
            with self.assertRaises(OutboxExecutionBudgetExceeded):
                webhook._ensure_welcome_audio_payload(payload)
        self.assertNotIn("skip_audio_generation", payload)

    def test_non_fixed_existing_audio_is_not_regenerated(self):
        payload = {"message_body": "Respuesta", "audio_url": "https://fixture.invalid/existing.mp3"}
        with patch.object(webhook, "generar_audio") as tts:
            webhook._ensure_welcome_audio_payload(payload)
        tts.assert_not_called()
        self.assertEqual(payload["audio_url"], "https://fixture.invalid/existing.mp3")


class RecordedTimer:
    def __init__(self, delay, callback):
        self.delay, self.callback, self.daemon = delay, callback, False

    def start(self):
        pass


class WelcomeOptionalAudioSignedRouteTestCase(unittest.TestCase):
    # Reuse the established tenant/sender fixture and real signature calculation,
    # without inheriting or rediscovering its unrelated queue tests.
    _signature = staticmethod(signed_fixture.WhatsAppInboundQueueWebhookTestCase._signature)
    _payload = staticmethod(signed_fixture.WhatsAppInboundQueueWebhookTestCase._payload)
    _post = signed_fixture.WhatsAppInboundQueueWebhookTestCase._post

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for target in ("socket.create_connection", "socket.socket.connect", "requests.sessions.Session.request", "urllib.request.urlopen"):
            self.stack.enter_context(patch(target, side_effect=AssertionError("External network prohibited in local fixture")))
        signed_fixture.WhatsAppInboundQueueWebhookTestCase.setUp(self)
        self.app.config["WHATSAPP_INBOUND_DURABILITY_MODE"] = "legacy"
        self.app.config.update(WELCOME_TEMPLATE_SID=None, WELCOME_MEDIA_URL=None, WELCOME_AUDIO_URL=None)
        self.owner.tipo_chat = "municipio"
        self.owner.rol = "municipio"
        self.owner.municipio_id = self.owner.id
        self.tenant.tipo = "municipio"
        self.tenant.municipio_id = self.owner.id
        self.tenant.pyme_id = None
        db.session.commit()
        self.provider = MagicMock()
        self.provider.messages.create.return_value = SimpleNamespace(sid="SM_local_provider_reply", status="queued")
        self.timers = []

        def capture_timer(delay, callback):
            timer = RecordedTimer(delay, callback)
            self.timers.append(timer)
            return timer

        self.stack.enter_context(patch.object(webhook, "Client", return_value=self.provider))
        self.stack.enter_context(patch.object(webhook, "_resolve_welcome_template_sid", return_value=None))
        self.stack.enter_context(patch.object(webhook, "resolve_contact", return_value=None))
        self.stack.enter_context(patch("services.config_loader.cargar_configuracion_municipio", return_value={}))
        self.stack.enter_context(patch("services.common_utils._get_main_menu_payload", side_effect=lambda *a, **k: menu_payload()))
        self.stack.enter_context(patch.object(webhook.threading, "Timer", side_effect=capture_timer))

    def tearDown(self):
        signed_fixture.WhatsAppInboundQueueWebhookTestCase.tearDown(self)

    def _known_greeting(self, suffix):
        payload = self._payload("SM_local_" + suffix, body="hola")
        payload["ProfileName"] = "Vecina Prueba"
        return payload

    def test_signed_greeting_TTS_exception_keeps_scheduled_text_menu(self):
        with patch.object(webhook, "generar_audio", side_effect=RuntimeError("local optional TTS failure")) as tts:
            response = self._post(self._known_greeting("throws"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.timers), 1)
            self.timers[0].callback()
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(self.provider.messages.create.call_count, 2)
        self.assertIn("Opciones municipales", self.provider.messages.create.call_args.kwargs["body"])

    def test_delayed_callback_TTS_exception_still_sends_text(self):
        with patch.object(webhook, "generar_audio", side_effect=RuntimeError("local callback TTS failure")) as tts:
            webhook._send_delayed_payload(self.provider, "whatsapp:+15550103030", "whatsapp:+15550909090", menu_payload(), 5, self.app)
            self.assertEqual(len(self.timers), 1)
            self.timers[0].callback()
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(self.provider.messages.create.call_count, 1)
        self.assertIn("Opciones municipales", self.provider.messages.create.call_args.kwargs["body"])

    def test_none_result_keeps_fallback_and_callback_does_not_retry(self):
        payload = menu_payload()
        payload["audio_url"] = "https://fixture.invalid/cached-menu.mp3"
        with patch.object(webhook, "generar_audio", return_value=None) as tts:
            webhook._ensure_welcome_audio_payload(payload)
            webhook._send_delayed_payload(self.provider, "whatsapp:+15550103030", "whatsapp:+15550909090", payload, 5, self.app)
            self.timers[0].callback()
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(self.provider.messages.create.call_count, 2)
        self.assertEqual(self.provider.messages.create.call_args.kwargs["media_url"], ["https://fixture.invalid/cached-menu.mp3"])

    def test_name_capture_TTS_exception_keeps_scheduled_text_menu(self):
        with patch.object(webhook, "generar_audio", side_effect=RuntimeError("local optional TTS failure")) as tts:
            response = self._post(self._payload("SM_local_awaiting", body="hola"))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(ChatSessionContext.query.one().context_data.get("awaiting_user_name"))
            with patch.object(webhook, "extract_multiple_contact_details_llm", return_value={"nombre": "Sofia"}), patch.object(webhook, "responder_chatboc", return_value=menu_payload()):
                response = self._post(self._payload("SM_local_name", body="Sofia"))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(self.timers), 1)
            self.timers[0].callback()
        self.assertEqual(tts.call_count, 1)
        self.assertEqual(self.provider.messages.create.call_count, 3)
        self.assertIn("Opciones municipales", self.provider.messages.create.call_args.kwargs["body"])

    def test_budget_control_still_propagates_from_delayed_callback(self):
        with patch.object(webhook, "generar_audio", side_effect=OutboxExecutionBudgetExceeded("outbox_execution_budget_exhausted")):
            webhook._send_delayed_payload(self.provider, "whatsapp:+15550103030", "whatsapp:+15550909090", menu_payload(), 5, self.app)
            with self.assertRaises(OutboxExecutionBudgetExceeded):
                self.timers[0].callback()
        self.provider.messages.create.assert_not_called()

    def test_pending_TTS_still_holds_ACK_until_released(self):
        # Explicit limitation: this patch does not move synthesis out of the ACK path.
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        response_data = {}

        def slow_tts(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise AssertionError("local test release not signalled")
            return None

        payload = self._known_greeting("pending")
        signature = self._signature(payload)

        def request_thread():
            try:
                with self.app.test_client() as client:
                    response_data["status"] = client.post("/webhook/whatsapp", data=payload, headers={"X-Twilio-Signature": signature}).status_code
            except Exception as exc:
                response_data["error"] = type(exc).__name__
            finally:
                done.set()

        with patch.object(webhook, "generar_audio", side_effect=slow_tts):
            thread = threading.Thread(target=request_thread, daemon=True)
            thread.start()
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(done.is_set())
                self.assertEqual(len(self.timers), 0)
            finally:
                release.set()
                thread.join(4)
        self.assertTrue(done.is_set())
        self.assertNotIn("error", response_data)
        self.assertEqual(response_data["status"], 200)
        self.assertEqual(len(self.timers), 1)


if __name__ == "__main__":
    unittest.main()
