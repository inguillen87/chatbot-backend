"""JUNÍ welcome HTTP contracts with disposable SQLite and a synthetic SDK.

The real webhook, session, formatter and outbound policy are exercised.  These
tests do not establish provider acceptance, delivery or durable-worker dispatch.
"""
from copy import deepcopy
from datetime import datetime as RealDatetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import g

from tests import test_whatsapp_webhook as webhook_fixture
from models import ChatSessionContext, db
from services.constants import CONTEXTO_MUNICIPIO
from services.whatsapp_inbound_worker import WhatsAppOutboundCollector
from utils.db_utils import safe_flag_modified
from routes import whatsapp_webhook as webhook


class _Clock(RealDatetime):
    offset = 0

    @classmethod
    def now(cls, tz=None):
        return RealDatetime.now(tz) + timedelta(seconds=cls.offset)


class _ImmediateTimer:
    def __init__(self, delay, callback):
        self.callback = callback

    def start(self):
        self.callback()


class JuniWelcomeEachGreetingTests(unittest.TestCase):
    def setUp(self):
        webhook_fixture.TestConfig.SESSION_TYPE = "null"
        webhook_fixture.TestConfig.RATELIMIT_STORAGE_URI = "memory://"
        self.fixture = webhook_fixture.WhatsAppWebhookTestCase("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.assertEqual(db.engine.url.database, ":memory:")
        self.tenant = self.fixture._attach_tenant_to_owner(slug="junin", tipo="municipio")
        self.fixture._create_confirmed_session()
        self.fixture.mock_validator.validate.return_value = True
        self.app = self.fixture.app
        self.app.config.update(
            WELCOME_TEMPLATE_SID=None,
            WELCOME_MEDIA_URL="https://api.chatboc.test/static/welcome/juni-saludo-sticker.webp",
            WELCOME_AUDIO_URL=None,
            WHATSAPP_MENU_AUDIO_ENABLED=False,
            WELCOME_MESSAGE_DELAY_SECONDS=0,
            WELCOME_STICKER_COOLDOWN_SECONDS=300,
        )
        self.calls = []
        self.fail_media = False
        self.collector = None
        _Clock.offset = 0

        def send(**kwargs):
            self.calls.append(dict(kwargs))
            if self.fail_media and kwargs.get("media_url"):
                raise TimeoutError("synthetic media boundary failure")
            return SimpleNamespace(sid="SM" + str(len(self.calls)).zfill(32), status="sent")

        self.fixture.mock_twilio_create.side_effect = send
        fallback_boundary = patch.object(
            webhook, "responder_chatboc",
            return_value={"message_body": "Synthetic fallback", "message_type": "text", "options_list": []},
        )
        self.fallback = fallback_boundary.start()
        self.addCleanup(fallback_boundary.stop)
        for boundary in (
            patch("datetime.datetime", _Clock),
            patch.object(webhook, "datetime", _Clock),
            patch.object(webhook.threading, "Timer", _ImmediateTimer),
            patch.object(webhook, "_prepare_cached_welcome_audio", return_value=None),
            patch.object(webhook, "_ensure_welcome_audio_payload", return_value=None),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

        @self.app.before_request
        def capture_outbound():
            if self.collector is not None:
                g.whatsapp_outbound_collector = self.collector

    def session(self):
        return ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).one()

    def context(self):
        db.session.expire_all()
        return self.session().context_data

    def save_context(self, data):
        row = self.session()
        row.context_data = deepcopy(data)
        safe_flag_modified(row, "context_data")
        db.session.commit()

    def post(self, number, offset=0, *, sid=True, profile=True, body="Hola"):
        _Clock.offset = offset
        before = len(self.calls)
        data = {
            "To": "whatsapp:" + self.fixture.test_whatsapp_number_str,
            "From": "whatsapp:" + self.fixture.test_user_number_str,
            "Body": body,
        }
        if sid:
            data["MessageSid"] = "SM" + str(number).zfill(32)
        if profile:
            data["ProfileName"] = "Persona Prueba"
        response = self.fixture.client.post(
            "/webhook/whatsapp", data=data,
            headers={"X-Twilio-Signature": "synthetic-signature"},
        )
        self.assertEqual(response.status_code, 200)
        return self.calls[before:]

    def assert_welcome(self, calls):
        self.assertEqual(len(calls), 3)
        sticker, greeting, menu = calls
        self.assertEqual(sticker.get("media_url"), [self.app.config["WELCOME_MEDIA_URL"]])
        self.assertFalse(sticker.get("body"))
        self.assertTrue("Acá" in greeting.get("body", "") or "Soy" in greeting.get("body", ""))
        self.assertTrue(menu.get("body"))
        self.assertFalse(menu.get("media_url"))
        for action in menu.get("persistent_action") or []:
            self.assertNotIn('"header"', action)

    def test_distinct_new_Hola_at_one_twenty_and_450_seconds_each_have_one_welcome(self):
        for number, offset in ((1001, 0), (1002, 1), (1003, 20), (1004, 450)):
            with self.subTest(offset=offset):
                self.assert_welcome(self.post(number, offset))
        self.fallback.assert_not_called()

    def test_same_MessageSid_replay_sends_nothing_but_fresh_sid_still_works(self):
        self.assert_welcome(self.post(1101))
        self.assertEqual(self.post(1101, 20), [])
        self.assert_welcome(self.post(1102, 21))

    def test_old_sticky_failure_does_not_disable_new_user_greeting(self):
        data = deepcopy(self.context())
        data["_welcome_state"] = {"sticker": {"disabled": True, "last_attempt_ts": RealDatetime.now().timestamp()}}
        self.save_context(data)
        self.assert_welcome(self.post(1201, 1))
        self.assertFalse(self.context()["_welcome_state"]["sticker"].get("disabled"))

    def test_current_media_failure_has_no_retry_and_preserves_text_then_new_turn(self):
        self.fail_media = True
        failed = self.post(1301)
        self.assert_welcome(failed)  # One attempted media call, then greeting/menu.
        self.assertEqual(len([call for call in failed if call.get("media_url")]), 1)
        self.assertFalse(self.context()["_welcome_state"]["sticker"].get("disabled"))
        self.fail_media = False
        self.assert_welcome(self.post(1302, 1))

    def test_unknown_name_does_not_hold_menu_behind_name_collection(self):
        self.assert_welcome(self.post(1401, profile=False))
        self.assertFalse(self.context().get("awaiting_user_name"))

    def test_missing_MessageSid_does_not_gain_new_repeat_policy(self):
        first = self.post(1501, sid=False)
        second = self.post(1502, 1, sid=False)
        self.assertEqual(sum(bool(call.get("media_url")) for call in first), 1)
        self.assertEqual(sum(bool(call.get("media_url")) for call in second), 0)

    def test_other_municipal_slug_cannot_gain_JUNI_policy_from_display_name(self):
        self.tenant.slug = "tdf"
        self.tenant.nombre = "Municipalidad de Junín"
        db.session.commit()
        self.assert_welcome(self.post(1601))
        second = self.post(1602, 20)
        self.assertEqual(sum(bool(call.get("media_url")) for call in second), 0)

    def test_owner_tenant_and_municipal_brand_bindings_are_required(self):
        owner = SimpleNamespace(id=7, municipio_id=None, tenant_id=9, tipo_chat="municipio")
        tenant = SimpleNamespace(id=9, municipio_id=7, slug="junin", tipo="municipio", is_active=True)
        self.assertTrue(webhook._juni_explicit_greeting_tenant(tenant, owner))
        for field, value in (("id", 10), ("municipio_id", 8), ("slug", "tdf"), ("tipo", "pyme"), ("is_active", False)):
            with self.subTest(field=field):
                changed = SimpleNamespace(**vars(tenant))
                setattr(changed, field, value)
                self.assertFalse(webhook._juni_explicit_greeting_tenant(changed, owner))

    def test_enterprise_policy_can_still_block_every_sdk_send(self):
        with patch.object(webhook, "_resolve_status_callback_tenant_and_sender", return_value=(self.tenant, None)), patch.object(
            webhook.WhatsAppEnterpriseRulesService, "reserve_outbound", return_value=(False, "hourly_limit_exceeded")
        ) as reserve:
            self.assertEqual(self.post(1701), [])
            self.assertEqual(self.post(1702, 1), [])
            self.assertGreaterEqual(reserve.call_count, 2)
        self.assertEqual(self.calls, [])

    def test_durable_capture_has_ordered_per_turn_keys_without_sdk_or_replay(self):
        for number, offset in ((1801, 0), (1802, 1)):
            self.collector = WhatsAppOutboundCollector(turn_id=f"synthetic-{number}")
            self.assertEqual(self.post(number, offset), [])
            staged = self.collector.outbound
            self.assertEqual(len(staged), 3)
            self.assertEqual(staged[0]["message_kind"], "media")
            self.assertEqual(staged[0]["payload"]["media_url"], [self.app.config["WELCOME_MEDIA_URL"]])
            self.assertEqual(len({item["idempotency_key"] for item in staged}), 3)
            self.assertEqual([item["idempotency_key"] for item in staged], [
                f"whatsapp-turn:synthetic-{number}:captured:{index}" for index in (1, 2, 3)
            ])
        self.collector = WhatsAppOutboundCollector(turn_id="synthetic-replay")
        self.assertEqual(self.post(1802, 20), [])
        self.assertEqual(self.collector.outbound, ())
        self.assertEqual(self.calls, [])

    def test_pending_direction_Hola_reemits_step_and_retains_options_and_draft(self):
        data = deepcopy(self.context())
        pending = {
            "estado_conversacion": "EN_FLUJO_RECLAMO",
            "reclamo_flow_v2": {
                "state": "ESPERANDO_DIRECCION", "confirmation_id": "synthetic-confirmation",
                "datos_reclamo": {"categoria": "Arbolado", "descripcion": "Árbol caído"},
            },
            "esperando_info_llm_reclamo": True,
        }
        options = [{"texto": "Menú", "action_id": "menu_principal"}, {"texto": "Cancelar", "action_id": "reclamo_cancelar"}]
        data[CONTEXTO_MUNICIPIO] = deepcopy(pending)
        data["last_options_sent"] = deepcopy(options)
        data["last_options_scope"] = "synthetic-current-step"
        self.save_context(data)
        calls = self.post(1901)
        self.assert_welcome(calls)
        self.assertIn("dirección exacta", calls[-1]["body"])
        current = self.context()
        for key, value in pending.items():
            self.assertEqual(current[CONTEXTO_MUNICIPIO][key], value)
        self.assertEqual(current["last_options_sent"], options)
        self.assertEqual(current["last_options_scope"], "synthetic-current-step")
        self.fallback.assert_not_called()

    def test_residual_name_onboarding_without_draft_returns_current_main_menu(self):
        data = deepcopy(self.context())
        data[CONTEXTO_MUNICIPIO] = {"estado_conversacion": "ESPERANDO_NOMBRE_INICIAL"}
        data["awaiting_user_name"] = True
        self.save_context(data)
        calls = self.post(2001)
        self.assert_welcome(calls)
        self.assertNotIn("Conservo lo que ya cargaste", calls[-1]["body"])
        current = self.context()
        self.assertFalse(current.get("awaiting_user_name"))
        self.assertNotEqual(current.get(CONTEXTO_MUNICIPIO, {}).get("estado_conversacion"), "ESPERANDO_NOMBRE_INICIAL")
        self.assertTrue(current.get("last_options_sent"))
        self.fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
