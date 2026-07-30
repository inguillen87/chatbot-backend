import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import (
    ChatSessionContext,
    ProviderSender,
    RealtimeToolCallReceipt,
    TenantProfile,
    User,
)
from services.voice_stream_service import (
    VoiceStreamService,
    _openai_realtime_headers,
    _realtime_tool_arguments_hash,
)


class _FakeSocket:
    def __init__(self):
        self.messages = []

    def send(self, payload):
        self.messages.append(json.loads(payload))


class VoiceStreamServiceMessageTests(unittest.TestCase):
    def test_audio_delta_forwards_media_to_twilio(self):
        for event_type in ("response.audio.delta", "response.output_audio.delta"):
            with self.subTest(event_type=event_type):
                twilio_ws = _FakeSocket()
                service = VoiceStreamService(twilio_ws)
                service.stream_sid = "stream-1"

                service.handle_openai_message({"type": event_type, "delta": "abc"})

                self.assertEqual(
                    twilio_ws.messages,
                    [{"event": "media", "streamSid": "stream-1", "media": {"payload": "abc"}}],
                )
                self.assertTrue(service.response_active)

    def test_barge_in_clears_twilio_and_cancels_openai_response(self):
        twilio_ws = _FakeSocket()
        openai_ws = _FakeSocket()
        service = VoiceStreamService(twilio_ws)
        service.stream_sid = "stream-2"
        service.openai_ws = openai_ws
        service.response_active = True
        service.response_id = "resp-1"

        service.handle_openai_message({"type": "input_audio_buffer.speech_started"})

        self.assertEqual(twilio_ws.messages, [{"event": "clear", "streamSid": "stream-2"}])
        self.assertEqual(openai_ws.messages, [{"type": "response.cancel", "response_id": "resp-1"}])
        self.assertFalse(service.response_active)
        self.assertTrue(service.cancel_pending)

    def test_demo_greeting_offers_requested_vertical_menu(self):
        service = VoiceStreamService(_FakeSocket())
        service.demo_hub = "chatboc"
        service.requested_vertical = "juni"

        greeting = service._build_chatboc_demo_greeting("Chatboc.ar Demo Hub", "Marcelo")

        self.assertIn("Hola Marcelo", greeting)
        self.assertIn("demo telefonica de municipios", greeting)
        self.assertIn("reclamo", greeting)

    def test_openai_realtime_headers_do_not_send_beta_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            headers = _openai_realtime_headers()

        self.assertIn("Authorization", headers)
        self.assertNotIn("OpenAI-Beta", headers)

    def test_openai_realtime_headers_ignore_legacy_beta_override(self):
        with patch.dict(
            "os.environ",
            {"OPENAI_REALTIME_ALLOW_BETA_HEADER": "true", "OPENAI_REALTIME_BETA_HEADER": "realtime=test"},
            clear=False,
        ):
            headers = _openai_realtime_headers()

        self.assertIn("Authorization", headers)
        self.assertNotIn("OpenAI-Beta", headers)

    def test_initial_voice_greeting_for_municipio_has_menu(self):
        service = VoiceStreamService(_FakeSocket())
        service.requested_vertical = "municipio"
        service.voice_vertical = "municipio"

        greeting = service._build_initial_voice_greeting("Municipalidad de Junin", "Marcelo")

        self.assertIn("Hola Marcelo", greeting)
        self.assertIn("Municipalidad de Junin", greeting)
        self.assertIn("reclamo", greeting.lower())
        self.assertIn("tramites", greeting.lower())

    def test_invalid_tool_arguments_return_structured_realtime_error(self):
        openai_ws = _FakeSocket()
        service = VoiceStreamService(_FakeSocket())
        service.openai_ws = openai_ws

        service.execute_tool(
            "call-invalid-json",
            "registrar_solicitud_operativa",
            '{"descripcion":"dato privado"',
        )

        self.assertEqual(len(openai_ws.messages), 2)
        output_item = openai_ws.messages[0]["item"]
        self.assertEqual(output_item["type"], "function_call_output")
        self.assertEqual(output_item["call_id"], "call-invalid-json")
        error_payload = json.loads(output_item["output"])
        self.assertFalse(error_payload["ok"])
        self.assertEqual(error_payload["error"]["code"], "invalid_arguments")
        self.assertFalse(error_payload["error"]["retryable"])
        self.assertNotIn("dato privado", output_item["output"])
        self.assertEqual(openai_ws.messages[1], {"type": "response.create"})

    def test_unknown_tool_name_is_hashed_in_logs(self):
        app = create_app(TestConfig)
        openai_ws = _FakeSocket()
        service = VoiceStreamService(_FakeSocket(), app=app)
        service.openai_ws = openai_ws
        private_tool_name = "persona_privada_32877851"

        with self.assertLogs("services.voice_stream_service", level="INFO") as captured:
            service.execute_tool(
                "call-private-tool-name",
                private_tool_name,
                "{}",
            )

        rendered_logs = "\n".join(captured.output)
        self.assertNotIn(private_tool_name, rendered_logs)
        self.assertIn("unavailable:", rendered_logs)
        error_payload = json.loads(openai_ws.messages[0]["item"]["output"])
        self.assertEqual(error_payload["error"]["code"], "tool_not_available")

    def test_tool_exception_returns_error_and_does_not_log_pii(self):
        app = create_app(TestConfig)
        openai_ws = _FakeSocket()
        service = VoiceStreamService(_FakeSocket(), app=app)
        service.openai_ws = openai_ws
        private_value = "dni-32877851-private"

        with patch.object(
            service,
            "_register_operational_request",
            side_effect=RuntimeError(private_value),
        ), patch.object(
            service,
            "_claim_realtime_tool_call",
            return_value=("execute", None),
        ), patch.object(
            service,
            "_mark_realtime_tool_call_unknown_for_session",
            side_effect=lambda **kwargs: kwargs["output"],
        ), self.assertLogs("services.voice_stream_service", level="ERROR") as captured:
            service.execute_tool(
                "call-tool-failure",
                "registrar_solicitud_operativa",
                json.dumps({"descripcion": private_value}),
            )

        error_payload = json.loads(openai_ws.messages[0]["item"]["output"])
        self.assertEqual(error_payload["error"]["code"], "tool_execution_unknown")
        self.assertFalse(error_payload["error"]["retryable"])
        self.assertNotIn(private_value, "\n".join(captured.output))
        self.assertNotIn(private_value, openai_ws.messages[0]["item"]["output"])

    def test_same_realtime_call_replays_result_and_conflicting_args_fail_closed(self):
        app = create_app(TestConfig)
        openai_ws = _FakeSocket()
        service = VoiceStreamService(_FakeSocket(), app=app)
        service.openai_ws = openai_ws
        arguments = json.dumps({"descripcion": "Arreglar luminaria"})

        with patch.object(
            service,
            "_register_operational_request",
            return_value="Solicitud registrada con seguimiento 42.",
        ) as execute_effect, patch.object(
            service,
            "_claim_realtime_tool_call",
            side_effect=[
                ("execute", None),
                ("replay", "Solicitud registrada con seguimiento 42."),
                ("conflict", None),
            ],
        ), patch.object(
            service,
            "_complete_realtime_tool_call",
            return_value="Solicitud registrada con seguimiento 42.",
        ) as complete_receipt:
            service.execute_tool(
                "call-replay-1",
                "registrar_solicitud_operativa",
                arguments,
            )
            service.execute_tool(
                "call-replay-1",
                "registrar_solicitud_operativa",
                arguments,
            )
            service.execute_tool(
                "call-replay-1",
                "registrar_solicitud_operativa",
                json.dumps({"descripcion": "Payload alterado"}),
            )

        execute_effect.assert_called_once()
        complete_receipt.assert_called_once()
        output_items = [
            message["item"]
            for message in openai_ws.messages
            if message.get("type") == "conversation.item.create"
        ]
        self.assertEqual(output_items[0]["output"], output_items[1]["output"])
        conflict = json.loads(output_items[2]["output"])
        self.assertEqual(conflict["error"]["code"], "tool_call_conflict")

    def test_realtime_create_actions_receive_stable_effect_idempotency_keys(self):
        app = create_app(TestConfig)

        claim_service = VoiceStreamService(_FakeSocket(), app=app)
        claim_service.openai_ws = _FakeSocket()
        claim_service.chat_session_id = "voice-claim-session"
        claim_service.from_number = "+5491112345678"
        claim_service.tenant_profile = SimpleNamespace(id=17, configuracion={})
        with patch(
            "services.actions.municipio_actions.CrearReclamoActionHandler"
        ) as claim_handler, patch(
            "services.voice_stream_service.resolve_contact",
            return_value={},
        ), patch.object(
            claim_service,
            "_claim_realtime_tool_call",
            return_value=("execute", None),
        ), patch.object(
            claim_service,
            "_complete_realtime_tool_call",
            side_effect=lambda *args, **kwargs: str(kwargs["output"]),
        ):
            claim_handler.return_value.execute.return_value = {
                "success": False,
                "message_body": "Faltan datos.",
            }
            claim_service.execute_tool(
                "call-create-claim",
                "crear_reclamo",
                json.dumps({"descripcion": "Poste caido"}),
            )

        claim_context = claim_handler.call_args.args[0]
        claim_args = claim_handler.return_value.execute.call_args.args[0]
        self.assertEqual(claim_context["tenant_id"], 17)
        self.assertTrue(claim_args["claim_confirmation_id"].startswith("voice:"))
        self.assertEqual(
            claim_context["idempotency_key"],
            claim_args["claim_confirmation_id"],
        )

        order_service = VoiceStreamService(_FakeSocket(), app=app)
        order_service.openai_ws = _FakeSocket()
        order_service.chat_session_id = "voice-order-session"
        order_service.tenant_profile = SimpleNamespace(id=29, configuracion={})
        with patch(
            "services.actions.pyme_order_actions.CrearPedidoAction"
        ) as order_handler, patch.object(
            order_service,
            "_claim_realtime_tool_call",
            return_value=("execute", None),
        ), patch.object(
            order_service,
            "_complete_realtime_tool_call",
            side_effect=lambda *args, **kwargs: str(kwargs["output"]),
        ):
            order_handler.return_value.execute.return_value = {
                "success": False,
                "message_body": "Faltan productos.",
            }
            order_service.execute_tool(
                "call-create-order",
                "crear_pedido",
                json.dumps({"productos": []}),
            )

        order_context = order_handler.call_args.args[0]
        self.assertEqual(order_context["tenant_id"], 29)
        self.assertTrue(order_context["idempotency_key"].startswith("voice:"))
        self.assertNotEqual(
            claim_args["claim_confirmation_id"],
            order_context["idempotency_key"],
        )


class VoiceStreamServiceTenantResolutionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _create_voice_scope(self, slug):
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
        session_id = f"{slug[:24]}-voice"
        session_context = ChatSessionContext(
            chat_session_id=session_id,
            user_id=owner.id,
            tenant_id=tenant.id,
            anon_id="+5491112345678",
            context_data={},
        )
        db.session.add(session_context)
        db.session.commit()
        return owner, tenant, session_context

    def test_provider_sender_routes_voice_to_dedicated_tenant(self):
        owner = User(
            email="owner-voice@example.com",
            name="Owner Voice",
            password_hash="test",
            rol="admin_pyme",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="voice-dedicated-tenant",
            nombre="Voice Dedicated Tenant",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        db.session.add(
            ProviderSender(
                tenant_id=tenant.id,
                channel="whatsapp",
                phone_number="+15551234567",
                sender_id="whatsapp:+15551234567",
                status="active",
            )
        )
        db.session.commit()

        service = VoiceStreamService(_FakeSocket(), app=self.app)

        resolved = service._resolve_context("+5492613168608", "+15551234567", "CAvoice1")

        self.assertTrue(resolved)
        self.assertEqual(service.tenant_profile.slug, "voice-dedicated-tenant")
        self.assertEqual(service.owner_user.email, "owner-voice@example.com")
        self.assertEqual(service.whatsapp_sender, "whatsapp:+15551234567")

    def test_realtime_tool_receipt_survives_service_reconnect(self):
        owner = User(
            email="owner-realtime-receipt@example.com",
            name="Owner Realtime Receipt",
            password_hash="test",
            rol="admin_pyme",
            tipo_chat="pyme",
        )
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="voice-realtime-receipt",
            nombre="Voice Realtime Receipt",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={},
        )
        db.session.add(tenant)
        db.session.flush()
        session_id = "voice_realtime_receipt_session"
        db.session.add(
            ChatSessionContext(
                chat_session_id=session_id,
                user_id=owner.id,
                tenant_id=tenant.id,
                anon_id="+5491112345678",
                context_data={},
            )
        )
        db.session.commit()

        first_ws = _FakeSocket()
        first_service = VoiceStreamService(_FakeSocket(), app=self.app)
        first_service.openai_ws = first_ws
        first_service.chat_session_id = session_id
        first_service.tenant_profile = tenant
        private_arguments = "direccion privada 123"
        arguments = json.dumps({"descripcion": private_arguments})
        def _assert_reserved_before_effect(*_args, **_kwargs):
            receipt = RealtimeToolCallReceipt.query.filter_by(
                tenant_id=tenant.id
            ).one()
            self.assertEqual(
                receipt.status,
                RealtimeToolCallReceipt.STATUS_RESERVED,
            )
            self.assertIsNone(receipt.output_text)
            return "Solicitud persistente numero 77."

        with patch.object(
            first_service,
            "_register_operational_request",
            side_effect=_assert_reserved_before_effect,
        ) as first_effect:
            first_service.execute_tool(
                "call-persisted-replay",
                "registrar_solicitud_operativa",
                arguments,
            )
        first_effect.assert_called_once()

        second_ws = _FakeSocket()
        second_service = VoiceStreamService(_FakeSocket(), app=self.app)
        second_service.openai_ws = second_ws
        second_service.chat_session_id = session_id
        second_service.tenant_profile = tenant
        with patch.object(
            second_service,
            "_register_operational_request",
            side_effect=AssertionError("effect must not run on replay"),
        ) as replay_effect:
            second_service.execute_tool(
                "call-persisted-replay",
                "registrar_solicitud_operativa",
                arguments,
            )

        replay_effect.assert_not_called()
        self.assertEqual(
            first_ws.messages[0]["item"]["output"],
            second_ws.messages[0]["item"]["output"],
        )
        second_service.execute_tool(
            "call-persisted-replay",
            "registrar_solicitud_operativa",
            json.dumps({"descripcion": "payload diferente"}),
        )
        output_items = [
            message["item"]
            for message in second_ws.messages
            if message.get("type") == "conversation.item.create"
        ]
        conflict = json.loads(output_items[-1]["output"])
        self.assertEqual(conflict["error"]["code"], "tool_call_conflict")
        db.session.expire_all()
        persisted = ChatSessionContext.query.filter_by(chat_session_id=session_id).one()
        receipts = persisted.context_data["realtime_tool_call_receipts_v1"]
        self.assertEqual(len(receipts), 1)
        self.assertNotIn(private_arguments, json.dumps(receipts))
        durable_receipt = RealtimeToolCallReceipt.query.filter_by(
            tenant_id=tenant.id
        ).one()
        self.assertEqual(
            durable_receipt.status,
            RealtimeToolCallReceipt.STATUS_COMPLETED,
        )
        self.assertEqual(
            durable_receipt.output_text,
            "Solicitud persistente numero 77.",
        )
        self.assertEqual(len(durable_receipt.session_id_hash), 64)
        self.assertEqual(len(durable_receipt.call_id_hash), 64)
        self.assertEqual(len(durable_receipt.arguments_hash), 64)
        self.assertNotIn(
            private_arguments,
            json.dumps(
                {
                    "session": durable_receipt.session_id_hash,
                    "call": durable_receipt.call_id_hash,
                    "arguments": durable_receipt.arguments_hash,
                    "output": durable_receipt.output_text,
                }
            ),
        )

    def test_reserved_realtime_call_is_never_reexecuted_blindly(self):
        _, tenant, session_context = self._create_voice_scope(
            "voice-reserved-receipt"
        )
        call_id = "call-left-reserved"
        tool_name = "registrar_solicitud_operativa"
        arguments = {"descripcion": "Revisar luminaria"}

        owner = VoiceStreamService(_FakeSocket(), app=self.app)
        owner.chat_session_id = session_context.chat_session_id
        owner.tenant_profile = tenant
        arguments_hash = _realtime_tool_arguments_hash(tool_name, arguments)
        effect_key = owner._tool_effect_idempotency_key(call_id)
        state, _ = owner._claim_realtime_tool_call(
            session_context,
            call_id=call_id,
            tool_name=tool_name,
            arguments_hash=arguments_hash,
            effect_idempotency_key=effect_key,
        )
        self.assertEqual(state, "execute")

        replay_ws = _FakeSocket()
        replay = VoiceStreamService(_FakeSocket(), app=self.app)
        replay.openai_ws = replay_ws
        replay.chat_session_id = session_context.chat_session_id
        replay.tenant_profile = tenant
        with patch.object(
            replay,
            "_register_operational_request",
            side_effect=AssertionError("reserved effect must not be retried"),
        ) as effect:
            replay.execute_tool(call_id, tool_name, json.dumps(arguments))

        effect.assert_not_called()
        error_payload = json.loads(replay_ws.messages[0]["item"]["output"])
        self.assertEqual(error_payload["error"]["code"], "tool_call_reserved")
        receipt = RealtimeToolCallReceipt.query.filter_by(
            tenant_id=tenant.id
        ).one()
        self.assertEqual(receipt.status, RealtimeToolCallReceipt.STATUS_RESERVED)

    def test_unknown_realtime_outcome_replays_error_without_duplicate_effect(self):
        _, tenant, session_context = self._create_voice_scope(
            "voice-unknown-receipt"
        )
        private_arguments = "dni privado 32877851"
        arguments = json.dumps({"descripcion": private_arguments})

        first_ws = _FakeSocket()
        first = VoiceStreamService(_FakeSocket(), app=self.app)
        first.openai_ws = first_ws
        first.chat_session_id = session_context.chat_session_id
        first.tenant_profile = tenant
        with patch.object(
            first,
            "_register_operational_request",
            side_effect=RuntimeError(private_arguments),
        ) as first_effect:
            first.execute_tool(
                "call-unknown-outcome",
                "registrar_solicitud_operativa",
                arguments,
            )
        first_effect.assert_called_once()
        first_output = first_ws.messages[0]["item"]["output"]
        first_error = json.loads(first_output)
        self.assertEqual(
            first_error["error"]["code"],
            "tool_execution_unknown",
        )

        db.session.expire_all()
        receipt = RealtimeToolCallReceipt.query.filter_by(
            tenant_id=tenant.id
        ).one()
        self.assertEqual(receipt.status, RealtimeToolCallReceipt.STATUS_UNKNOWN)
        self.assertEqual(receipt.output_text, first_output)
        self.assertNotIn(private_arguments, "|".join(filter(None, [
            receipt.session_id_hash,
            receipt.call_id_hash,
            receipt.arguments_hash,
            receipt.effect_idempotency_key,
            receipt.output_text,
            receipt.last_error_code,
        ])))

        replay_ws = _FakeSocket()
        replay = VoiceStreamService(_FakeSocket(), app=self.app)
        replay.openai_ws = replay_ws
        replay.chat_session_id = session_context.chat_session_id
        replay.tenant_profile = tenant
        with patch.object(
            replay,
            "_register_operational_request",
            side_effect=AssertionError("unknown effect must not be retried"),
        ) as replay_effect:
            replay.execute_tool(
                "call-unknown-outcome",
                "registrar_solicitud_operativa",
                arguments,
            )
        replay_effect.assert_not_called()
        self.assertEqual(replay_ws.messages[0]["item"]["output"], first_output)

    def test_realtime_receipt_bounds_replayed_output(self):
        _, tenant, session_context = self._create_voice_scope(
            "voice-bounded-output"
        )
        service_ws = _FakeSocket()
        service = VoiceStreamService(_FakeSocket(), app=self.app)
        service.openai_ws = service_ws
        service.chat_session_id = session_context.chat_session_id
        service.tenant_profile = tenant

        with patch.object(
            service,
            "_register_operational_request",
            return_value="x" * 5000,
        ):
            service.execute_tool(
                "call-bounded-output",
                "registrar_solicitud_operativa",
                json.dumps({"descripcion": "Prueba"}),
            )

        output = service_ws.messages[0]["item"]["output"]
        self.assertEqual(len(output), 4096)
        receipt = RealtimeToolCallReceipt.query.filter_by(
            tenant_id=tenant.id
        ).one()
        self.assertEqual(len(receipt.output_text), 4096)


if __name__ == "__main__":
    unittest.main()
