import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import json
import time
import copy

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SKIP_INIT_TENANTS", "1")

from flask import g

# Añadir el directorio raíz del proyecto al sys.path
project_root_whatsapp = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_whatsapp not in sys.path:
    sys.path.insert(0, project_root_whatsapp)

from app import create_app, db
from config import Config
from models import User, Rubro, WhatsappNumero, ChatSessionContext, PymeTicket, TenantProfile
from models_memory import Contact, InteractionEvent
from services.municipio_responder import CONTEXTO_MUNICIPIO
from routes.whatsapp_webhook import (
    _send_delayed_payload,
    _strip_duplicate_welcome_media,
    _reset_municipio_context_for_menu,
    CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER,
    CHATBOC_DEMO_TENANT_SLUG,
)
# Moved model imports after app and config to ensure they are found via sys.path
# and to avoid potential issues if models.py itself tries to import app-context related things early.
# However, for direct use in tests, they are typically at the top. Let's try keeping them here.
import models

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:' # Use in-memory SQLite for tests
    WTF_CSRF_ENABLED = False
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    CHATBOC_DEMO_WHATSAPP_NUMBERS = "+19999999999"
    CHATBOC_DEMO_MAX_MESSAGES = 10
    TWILIO_ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test" # Mock SID
    TWILIO_AUTH_TOKEN = "your_auth_token_test" # Mock Token
    # TWILIO_NUMEROS_JSON is no longer used

class WhatsAppWebhookTestCase(unittest.TestCase):

    def setUp(self):
        os.environ["TWILIO_ACCOUNT_SID"] = TestConfig.TWILIO_ACCOUNT_SID
        os.environ["TWILIO_AUTH_TOKEN"] = TestConfig.TWILIO_AUTH_TOKEN
        os.environ.setdefault("OPENAI_API_KEY", "test")

        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all() # Create all tables, including whatsapp_numero
        self.client = self.app.test_client()

        # Test data
        self.test_whatsapp_number_str = "+15551234567"
        self.test_user_number_str = "+15557654321"

        # Create a mock User (company/municipality)
        self.mock_client_user = User(
            name="TestEmpresa",
            email="testempresa@example.com",
            rol="empresa", # or 'municipio'
            tipo_chat="pyme", # or 'municipio'
            pyme_id=1,
            nombre_empresa="TestEmpresaName"
        )
        self.mock_client_user.set_password("testpassword")
        db.session.add(self.mock_client_user)
        db.session.commit() # Commit to get an ID for mock_client_user

        self.empresa_id_for_test = self.mock_client_user.id
        self.client_name_for_test = self.mock_client_user.nombre_empresa
        self.client_type_for_test = self.mock_client_user.tipo_chat

        # Create a mock WhatsappNumero mapping
        self.mock_whatsapp_mapping = WhatsappNumero(
            numero_whatsapp=self.test_whatsapp_number_str,
            user_id=self.mock_client_user.id,
            is_active=True
        )
        db.session.add(self.mock_whatsapp_mapping)
        db.session.commit()

        # Patch the RequestValidator (globally for all tests in this class)
        self.validator_patch = patch('routes.whatsapp_webhook.validator', MagicMock())
        self.mock_validator = self.validator_patch.start()

        # Patch the twilio_client
        self.twilio_client_patch = patch('routes.whatsapp_webhook.twilio_client', MagicMock())
        self.mock_twilio_client = self.twilio_client_patch.start()
        self.mock_twilio_create = self.mock_twilio_client.messages.create

        # Provide a placeholder for the old welcome helper so assertions
        # referencing it don't break even though production code no longer uses it.
        self.welcome_patch = patch(
            'routes.whatsapp_webhook.enviar_bienvenida_whatsapp',
            MagicMock(),
            create=True,
        )
        self.mock_welcome = self.welcome_patch.start()

    def _set_owner_tipo_chat(self, tipo: str) -> None:
        self.mock_client_user.tipo_chat = tipo
        db.session.add(self.mock_client_user)
        db.session.commit()

    def _create_confirmed_session(self):
        session_context = ChatSessionContext(
            chat_session_id=f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}",
            user_id=self.empresa_id_for_test,
            anon_id=self.test_user_number_str,
            context_data={
                "historial_chat": [],
                "estado_conversacion": "activo",
                "user_id_empresa": self.empresa_id_for_test,
                "telefono_usuario": self.test_user_number_str,
                "canal_origen": "whatsapp",
                "mensajes_previos_llm_formato": [],
                "perfil_confirmado": True,
            },
        )
        db.session.add(session_context)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.validator_patch.stop()
        self.twilio_client_patch.stop()
        self.welcome_patch.stop()

    def test_reset_municipio_context_for_menu_clears_sensitive_draft_data(self):
        session = ChatSessionContext(
            chat_session_id="ctx-reset-test",
            user_id=self.empresa_id_for_test,
            anon_id=self.test_user_number_str,
            context_data={
                CONTEXTO_MUNICIPIO: {
                    "estado_conversacion": "confirmando_reclamo",
                    "reclamo_flow_v2": {"state": "confirm"},
                    "datos_reclamo": {"categoria": "Bache"},
                    "datos_parciales_llm_reclamo": {"descripcion": "calle rota"},
                    "confirmation_required": True,
                },
                "last_options_sent": [{"texto": "Iniciar reclamo", "action_id": "iniciar_reclamo"}],
                "pending_sensitive_action": {"action_id": "iniciar_reclamo"},
            },
        )
        db.session.add(session)
        db.session.commit()

        _reset_municipio_context_for_menu(session)

        municipio_ctx = session.context_data.get(CONTEXTO_MUNICIPIO, {})
        self.assertEqual(municipio_ctx.get("estado_conversacion"), "ESPERANDO_SELECCION_MENU_PRINCIPAL")
        self.assertNotIn("reclamo_flow_v2", municipio_ctx)
        self.assertNotIn("datos_reclamo", municipio_ctx)
        self.assertNotIn("datos_parciales_llm_reclamo", municipio_ctx)
        self.assertNotIn("last_options_sent", session.context_data)
        self.assertNotIn("pending_sensitive_action", session.context_data)

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_claim_numeric_menu_option_starts_without_extra_confirmation(self, mock_bot):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        mock_bot.return_value = {
            "message_body": "Elegí una categoría para tu reclamo.",
            "message_type": "text",
            "options_list": [],
        }

        self._create_confirmed_session()
        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ).first()
        updated_context = dict(session.context_data or {})
        updated_context["last_options_sent"] = [
            {"texto": "Iniciar un Reclamo", "action_id": "iniciar_reclamo"}
        ]
        session.context_data = updated_context
        db.session.add(session)
        db.session.commit()

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "1",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")

        mock_bot.assert_called_once()
        self.assertGreaterEqual(self.mock_twilio_create.call_count, 1)
        sent_body = self.mock_twilio_create.call_args.kwargs.get("body", "")
        self.assertNotIn("confirmame por favor", sent_body.lower())

        db.session.refresh(session)
        self.assertNotIn("pending_sensitive_action", session.context_data)

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_number_routes_to_platform_hub_and_records_lead(self, mock_bot):
        self.mock_validator.validate.return_value = True

        payload = {
            "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
            "From": "whatsapp:+5492613168608",
            "Body": "hola",
            "ProfileName": "Marcelo",
            "MessageSid": "SM_CHATBOC_DEMO_1",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")
        mock_bot.assert_not_called()

        mapping = WhatsappNumero.query.filter_by(
            numero_whatsapp=CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER
        ).first()
        self.assertIsNotNone(mapping)
        self.assertTrue(mapping.is_active)
        self.assertEqual(mapping.user.rol, "super_admin")

        tenant = TenantProfile.query.filter_by(slug=CHATBOC_DEMO_TENANT_SLUG).first()
        self.assertIsNotNone(tenant)
        self.assertTrue((tenant.configuracion or {}).get("whatsapp_demo_hub"))

        contact = User.query.filter_by(
            telefono="+5492613168608",
            empresa_id=mapping.user_id,
        ).first()
        self.assertIsNotNone(contact)
        self.assertIn("chatboc_demo", contact.tags)

        ticket = PymeTicket.query.filter_by(categoria="chatboc_demo_lead").first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.telefono, "+5492613168608")

        crm_contact = Contact.query.filter_by(
            tenant_id=tenant.id,
            phone="+5492613168608",
        ).first()
        self.assertIsNotNone(crm_contact)
        self.assertIn("chatboc_demo", crm_contact.tags)
        self.assertEqual((crm_contact.preferences or {}).get("marketing_consent_status"), "unknown")

        interaction = InteractionEvent.query.filter_by(
            tenant_id=tenant.id,
            contact_id=crm_contact.id,
            channel="whatsapp",
            direction="inbound",
        ).first()
        self.assertIsNotNone(interaction)
        self.assertEqual((interaction.metadata_payload or {}).get("event_type"), "chatboc_demo_inbound")

        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{mapping.user_id}_+5492613168608"
        ).first()
        self.assertIsNotNone(session)
        self.assertEqual((session.context_data or {}).get("estado_conversacion"), "chatboc_demo_hub")
        self.assertEqual(((session.context_data or {}).get("chatboc_demo_usage") or {}).get("message_count"), 1)

        sent_bodies = [
            str(call.kwargs.get("body") or "")
            for call in self.mock_twilio_create.call_args_list
        ]
        self.assertTrue(any("Chatboc.ar" in body for body in sent_bodies))
        self.assertTrue(any("Municipio inteligente" in body for body in sent_bodies))

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_business_free_text_order_keeps_context(self, mock_bot):
        self.mock_validator.validate.return_value = True

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_ORDER_1").status_code, 200)
        self.assertEqual(send_demo("3", "SM_CHATBOC_DEMO_ORDER_2").status_code, 200)
        response = send_demo("quieor hacer un pedido", "SM_CHATBOC_DEMO_ORDER_3")

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_not_called()
        sent_bodies = [
            str(call.kwargs.get("body") or "")
            for call in self.mock_twilio_create.call_args_list
        ]
        self.assertTrue(any("Pedido demo empresas" in body for body in sent_bodies))
        self.assertTrue(any("Confirmar pedido demo" in body for body in sent_bodies))

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_authorized_reset_number_can_reopen_menu_after_limit(self, mock_bot):
        self.mock_validator.validate.return_value = True
        self.app.config["CHATBOC_DEMO_MAX_MESSAGES"] = 2
        self.app.config["CHATBOC_DEMO_RESET_WHATSAPP_NUMBERS"] = "+5492613168608"

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_RESET_1").status_code, 200)
        self.assertEqual(send_demo("3", "SM_CHATBOC_DEMO_RESET_2").status_code, 200)
        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_RESET_3").status_code, 200)
        mock_bot.assert_not_called()

        sent_bodies = [str(call.kwargs.get("body") or "") for call in self.mock_twilio_create.call_args_list]
        self.assertNotIn("Llegaste al limite", sent_bodies[-1])
        self.assertIn("Municipio inteligente", sent_bodies[-1])

        session = ChatSessionContext.query.filter_by(anon_id="+5492613168608").first()
        self.assertIsNotNone(session)
        usage = (session.context_data or {}).get("chatboc_demo_usage") or {}
        self.assertEqual(usage.get("message_count"), 0)
        self.assertEqual(usage.get("last_reset_reason"), "limit_navigation")

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_public_number_stays_limited_after_limit(self, mock_bot):
        self.mock_validator.validate.return_value = True
        self.app.config["CHATBOC_DEMO_MAX_MESSAGES"] = 2
        self.app.config["CHATBOC_DEMO_RESET_WHATSAPP_NUMBERS"] = "+5491111111111"

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_LIMIT_1").status_code, 200)
        self.assertEqual(send_demo("3", "SM_CHATBOC_DEMO_LIMIT_2").status_code, 200)
        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_LIMIT_3").status_code, 200)
        mock_bot.assert_not_called()

        sent_bodies = [str(call.kwargs.get("body") or "") for call in self.mock_twilio_create.call_args_list]
        self.assertIn("Llegaste al limite", sent_bodies[-1])
        self.assertNotIn("Municipio inteligente", sent_bodies[-1])

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_school_free_text_keeps_context(self, mock_bot):
        self.mock_validator.validate.return_value = True

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_SCHOOL_1").status_code, 200)
        self.assertEqual(send_demo("2", "SM_CHATBOC_DEMO_SCHOOL_2").status_code, 200)
        response = send_demo("quiero consultar admisiones", "SM_CHATBOC_DEMO_SCHOOL_3")

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_not_called()
        sent_bodies = [
            str(call.kwargs.get("body") or "")
            for call in self.mock_twilio_create.call_args_list
        ]
        self.assertTrue(any("Consulta colegio demo" in body for body in sent_bodies))
        self.assertTrue(any("Abrir demo colegios" in body for body in sent_bodies))

    @patch("routes.whatsapp_webhook.responder_chatboc")
    @patch("services.audio_transcription_service.transcribe_audio_from_url")
    @patch("routes.whatsapp_webhook.create_attachment_with_thumbnail")
    @patch("routes.whatsapp_webhook.requests.get")
    def test_chatboc_demo_voice_note_is_transcribed_routed_and_saved_to_crm(
        self,
        mock_requests_get,
        mock_create_attachment,
        mock_transcribe,
        mock_bot,
    ):
        self.mock_validator.validate.return_value = True
        mock_response = MagicMock()
        mock_response.content = b"fake audio"
        mock_response.raise_for_status.return_value = None
        mock_requests_get.return_value = mock_response
        attachment = MagicMock()
        attachment.id = 701
        attachment.url = "https://cdn.chatboc.ar/demo/audio.ogg"
        attachment.mime = "audio/ogg"
        attachment.nombre_original = "nota_de_voz.ogg"
        attachment.analisis = None
        mock_create_attachment.return_value = attachment
        mock_transcribe.return_value = "quiero hacer un pedido"

        payload = {
            "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
            "From": "whatsapp:+5492613168608",
            "Body": "",
            "ProfileName": "Marcelo",
            "MessageSid": "SM_CHATBOC_DEMO_AUDIO",
            "MediaMessageSid": "MM_CHATBOC_DEMO_AUDIO",
            "MediaUrl0": "https://api.twilio.com/media/audio.ogg",
            "MediaContentType0": "audio/ogg",
        }
        response = self.client.post(
            "/webhook/whatsapp",
            data=payload,
            headers={"X-Twilio-Signature": "dummy_signature_valid"},
        )

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_not_called()
        sent_bodies = [str(call.kwargs.get("body") or "") for call in self.mock_twilio_create.call_args_list]
        self.assertTrue(any("Pedido demo empresas" in body for body in sent_bodies))

        interaction = InteractionEvent.query.filter_by(content_type="audio").first()
        self.assertIsNotNone(interaction)
        self.assertIn("quiero hacer un pedido", interaction.content)
        metadata = interaction.metadata_payload or {}
        self.assertEqual(metadata.get("content_type"), "audio")
        self.assertEqual(((metadata.get("media") or {}).get("attachment_id")), 701)

        crm_contact = Contact.query.filter_by(phone="+5492613168608").first()
        self.assertEqual((crm_contact.preferences or {}).get("last_demo_input_type"), "audio")

    @patch("routes.whatsapp_webhook.responder_chatboc")
    @patch("routes.whatsapp_webhook.create_attachment_with_thumbnail")
    @patch("routes.whatsapp_webhook.requests.get")
    def test_chatboc_demo_image_is_kept_as_reclamo_evidence(
        self,
        mock_requests_get,
        mock_create_attachment,
        mock_bot,
    ):
        self.mock_validator.validate.return_value = True

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_IMAGE_1").status_code, 200)
        self.assertEqual(send_demo("1", "SM_CHATBOC_DEMO_IMAGE_2").status_code, 200)

        mock_response = MagicMock()
        mock_response.content = b"fake image"
        mock_response.raise_for_status.return_value = None
        mock_requests_get.return_value = mock_response
        attachment = MagicMock()
        attachment.id = 702
        attachment.url = "https://cdn.chatboc.ar/demo/foto.jpg"
        attachment.mime = "image/jpeg"
        attachment.nombre_original = "foto.jpg"
        attachment.analisis = None
        mock_create_attachment.return_value = attachment

        response = self.client.post(
            "/webhook/whatsapp",
            data={
                "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                "From": "whatsapp:+5492613168608",
                "Body": "",
                "ProfileName": "Marcelo",
                "MessageSid": "SM_CHATBOC_DEMO_IMAGE_3",
                "MediaMessageSid": "MM_CHATBOC_DEMO_IMAGE",
                "MediaUrl0": "https://api.twilio.com/media/foto.jpg",
                "MediaContentType0": "image/jpeg",
            },
            headers={"X-Twilio-Signature": "dummy_signature_valid"},
        )

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_not_called()
        sent_bodies = [str(call.kwargs.get("body") or "") for call in self.mock_twilio_create.call_args_list]
        self.assertTrue(any("Evidencia recibida para demo municipio" in body for body in sent_bodies))

        interaction = InteractionEvent.query.filter_by(content_type="image").first()
        self.assertIsNotNone(interaction)
        self.assertEqual(interaction.media_url, "https://cdn.chatboc.ar/demo/foto.jpg")
        self.assertEqual(((interaction.metadata_payload or {}).get("media") or {}).get("attachment_id"), 702)

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_chatboc_demo_location_is_saved_and_contextualized(self, mock_bot):
        self.mock_validator.validate.return_value = True

        def send_demo(body: str, sid: str):
            return self.client.post(
                "/webhook/whatsapp",
                data={
                    "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                    "From": "whatsapp:+5492613168608",
                    "Body": body,
                    "ProfileName": "Marcelo",
                    "MessageSid": sid,
                },
                headers={"X-Twilio-Signature": "dummy_signature_valid"},
            )

        self.assertEqual(send_demo("hola", "SM_CHATBOC_DEMO_LOCATION_1").status_code, 200)
        self.assertEqual(send_demo("1", "SM_CHATBOC_DEMO_LOCATION_2").status_code, 200)

        response = self.client.post(
            "/webhook/whatsapp",
            data={
                "To": f"whatsapp:{CHATBOC_DEMO_DEFAULT_WHATSAPP_NUMBER}",
                "From": "whatsapp:+5492613168608",
                "Body": "",
                "ProfileName": "Marcelo",
                "MessageSid": "SM_CHATBOC_DEMO_LOCATION_3",
                "Latitude": "-33.08392315",
                "Longitude": "-68.47351916",
                "Address": "25 de Mayo 19, San Martin, Mendoza",
            },
            headers={"X-Twilio-Signature": "dummy_signature_valid"},
        )

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_not_called()
        sent_bodies = [str(call.kwargs.get("body") or "") for call in self.mock_twilio_create.call_args_list]
        self.assertTrue(any("Ubicacion recibida para demo municipio" in body for body in sent_bodies))
        self.assertTrue(any("25 de Mayo 19" in body for body in sent_bodies))

        interaction = InteractionEvent.query.filter_by(content_type="location").first()
        self.assertIsNotNone(interaction)
        metadata = interaction.metadata_payload or {}
        self.assertEqual(metadata.get("content_type"), "location")
        self.assertEqual(((metadata.get("location") or {}).get("address")), "25 de Mayo 19, San Martin, Mendoza")

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_includes_audio(self, mock_build_response, mock_timer):
        """Ensure delayed payloads also send synthesized audio attachments."""

        self.app.config["APP_BASE_URL"] = "https://example.com"

        payload = {
            "message_body": "Hola, este es el menú.",
            "options_list": [{"texto": "Opción", "id": "opcion"}],
            "message_type": "text",
            "audio_url": "/static/audio/menu.mp3",
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Mensaje principal"},
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.delay = delay
                self.callback = callback
                self.daemon = False

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        payload.setdefault("_base_url", self.app.config["APP_BASE_URL"])
        payload.setdefault("_request_url_root", self.app.config["APP_BASE_URL"])

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        # First call is the main message, second the audio attachment.
        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["body"], "Mensaje principal")
        self.assertEqual(
            sent_messages[1]["media_url"],
            ["https://example.com/static/audio/menu.mp3"],
        )
        mock_build_response.assert_called_once()
        self.assertEqual(
            mock_build_response.call_args.kwargs["audio_url"],
            payload["audio_url"],
        )

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_sends_pre_messages_first(
        self, mock_build_response, mock_timer
    ):
        """Pre-messages should be sent before the delayed interactive payload."""

        self.app.config["APP_BASE_URL"] = "https://example.com"

        payload = {
            "message_body": "Hola, este es el menú.",
            "options_list": [{"texto": "Opción", "id": "opcion"}],
            "message_type": "text",
            "_base_url": "https://example.com",
            "_request_url_root": "https://example.com/",
            "_twilio_pre_messages": [
                {
                    "channels": ["whatsapp"],
                    "body": "Encuestas/Opiniones/Sondeos",
                    "media_urls": ["/static/encuestas/banner.png"],
                },
                {
                    "channels": ["sms"],
                    "body": "No enviar",
                },
            ],
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Mensaje principal"},
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertEqual(len(sent_messages), 2)
        banner_message, main_message = sent_messages
        self.assertEqual(banner_message.get("body"), "Encuestas/Opiniones/Sondeos")
        self.assertEqual(
            banner_message.get("media_url"),
            ["https://example.com/static/encuestas/banner.png"],
        )
        self.assertEqual(main_message.get("body"), "Mensaje principal")

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_skips_welcome_sticker_media(self, mock_build_response, mock_timer):
        """Delayed payload should not resend the welcome sticker media."""

        sticker_url = "https://example.com/static/welcome/sticker.webp"
        payload = {
            "message_body": "Hola, este es el menú.",
            "message_type": "text",
            "_base_url": "https://example.com",
            "_request_url_root": "https://example.com/",
            "_welcome_sticker_urls": [sticker_url],
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Mensaje principal"},
            "image_url": sticker_url,
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertEqual(len(sent_messages), 1)
        self.assertNotIn("media_url", sent_messages[0])

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_skips_cross_domain_sticker(self, mock_build_response, mock_timer):
        """Sticker URLs should be skipped even if the domain differs."""

        sticker_url = "https://example.com/static/welcome/sticker.webp"
        other_domain_url = "https://cdn.example.org/media/welcome/STICKER.WEBP"
        payload = {
            "message_body": "Hola, este es el menú.",
            "message_type": "text",
            "_base_url": "https://example.com",
            "_request_url_root": "https://example.com/",
            "_welcome_sticker_urls": [sticker_url],
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Mensaje principal"},
            "image_url": other_domain_url,
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertEqual(len(sent_messages), 1)
        self.assertNotIn("media_url", sent_messages[0])

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_strips_sticker_header(self, mock_build_response, mock_timer):
        """Interactive headers using the welcome sticker must be removed."""

        sticker_url = "https://example.com/static/welcome/sticker.webp"
        payload = {
            "message_body": "Hola, este es el menú.",
            "message_type": "interactive_menu",
            "options_list": [{"texto": "Opción", "id": "opcion"}],
            "_base_url": "https://example.com",
            "_request_url_root": "https://example.com/",
            "_welcome_sticker_urls": [sticker_url],
        }

        mock_build_response.return_value = {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "Menú"},
                "header": {"type": "image", "image": {"link": sticker_url}},
                "action": {"sections": []},
            },
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertEqual(len(sent_messages), 1)
        params = sent_messages[0]
        self.assertIn("persistent_action", params)
        action_payload = params["persistent_action"][0]
        self.assertTrue(action_payload.startswith("whatsapp:"))
        serialized = action_payload.split("whatsapp:", 1)[1]
        interactive_json = json.loads(serialized)
        self.assertNotIn("header", interactive_json)

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_send_delayed_payload_adds_header_when_preserve_flag(self, mock_build_response, mock_timer):
        sticker_url = "https://example.com/static/welcome/sticker.webp"
        payload = {
            "message_body": "Hola, este es el menú.",
            "message_type": "interactive_menu",
            "options_list": [{"texto": "Opción", "id": "opcion"}],
            "_base_url": "https://example.com",
            "_request_url_root": "https://example.com/",
            "_welcome_sticker_urls": [sticker_url],
            "_preserve_welcome_header": True,
        }

        mock_build_response.return_value = {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": "Menú"},
                "action": {"sections": []},
            },
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111111111",
            from_number="whatsapp:+222222222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertEqual(len(sent_messages), 1)
        params = sent_messages[0]
        self.assertIn("persistent_action", params)
        interactive_json = json.loads(params["persistent_action"][0].split("whatsapp:", 1)[1])
        header = interactive_json.get("header", {})
        self.assertEqual(header.get("type"), "image")
        self.assertEqual(header.get("image", {}).get("link"), sticker_url)

    def test_whatsapp_webhook_valid_request(self):
        # Arrange
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")
        self.mock_validator.validate.assert_called_once()

        # The webhook should send the template, the sticker media and the greeting text,
        # so messages.create is invoked three times. The template call must include an
        # empty string for the name placeholder when unknown.
        self.assertEqual(self.mock_twilio_create.call_count, 3)
        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertIn("content_variables", template_kwargs)

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        expected_media = [self.app.config["WELCOME_MEDIA_URL"]]
        self.assertEqual(sticker_kwargs.get("media_url"), expected_media)
        self.assertNotIn("persistent_action", sticker_kwargs)

        greeting_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("body", greeting_kwargs)

        # Legacy welcome helper is no longer used.
        self.mock_welcome.assert_not_called()

    def test_pyme_welcome_skips_template_and_sticker(self):
        self._set_owner_tipo_chat("pyme")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 1)
        greeting_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertIn("body", greeting_kwargs)
        self.assertNotIn("media_url", greeting_kwargs)

        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        welcome_state = ctx.context_data.get("_welcome_state", {}) if ctx else {}
        self.assertFalse(welcome_state.get("sticker", {}).get("last_sent_ts"))
        self.assertFalse(welcome_state.get("template", {}).get("last_sent_ts"))

    def test_pyme_welcome_uses_rubro_overrides(self):
        self._set_owner_tipo_chat("pyme")
        rubro = Rubro(clave="bodega", nombre="Bodega")
        db.session.add(rubro)
        db.session.commit()

        self.mock_client_user.rubro = rubro
        self.mock_client_user.rubro_id = rubro.id
        db.session.add(self.mock_client_user)
        db.session.commit()

        self.mock_validator.validate.return_value = True
        self.app.config["APP_BASE_URL"] = "https://chatboc.ar"
        self.app.config["WELCOME_TEMPLATE_SID"] = "municipio_template"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/municipio.webp"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        override_config = {
            "nombre_pyme": "Bodega Cuatro Fincas",
            "whatsapp": {"numero": "+5492613168608"},
            "welcome": {
                "template_sid": "HX33b306d980ee328f7893db484ba349c4",
                "template_variables": {"1": "{{pyme_whatsapp}}"},
                "sticker_url": "/static/welcome/saludo_media_cuatrofincas.webp",
                "sticker_cooldown_seconds": 120,
            },
        }

        def _mock_loader(slug, archivo):
            if archivo != "config.json":
                return {}
            if slug == "bodega":
                return override_config
            if slug == "default":
                return {"welcome": {}}
            return {}

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch("routes.whatsapp_webhook.cargar_configuracion_pyme", side_effect=_mock_loader):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertEqual(template_kwargs.get("content_sid"), override_config["welcome"]["template_sid"])
        content_vars = json.loads(template_kwargs.get("content_variables"))
        self.assertEqual(content_vars, {"1": "+5492613168608"})

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(
            sticker_kwargs.get("media_url"),
            ["https://chatboc.ar/static/welcome/saludo_media_cuatrofincas.webp"],
        )

        greeting_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("body", greeting_kwargs)

    def test_sticker_respects_cooldown(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMwelcome"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        first = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        ctx.context_data["last_welcome_ts"] = ctx.context_data.get("last_welcome_ts", 0) - 60
        db.session.add(ctx)
        db.session.commit()

        self.mock_twilio_create.reset_mock()
        second = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(second.status_code, 200)
        # Sticker should be skipped; remaining sends must not include media.
        self.assertGreaterEqual(self.mock_twilio_create.call_count, 1)
        for call in self.mock_twilio_create.call_args_list:
            self.assertNotIn("media_url", call.kwargs)

    def test_webhook_finds_mapping_without_plus_prefix(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"

        # Store the number without the leading '+' to emulate inconsistent data.
        self.mock_whatsapp_mapping.numero_whatsapp = self.test_whatsapp_number_str.replace("+", "")
        db.session.add(self.mock_whatsapp_mapping)
        db.session.commit()

        self._create_confirmed_session()
        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        ctx.context_data["last_welcome_ts"] = time.time()
        db.session.add(ctx)
        db.session.commit()

        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch("routes.whatsapp_webhook._send_delayed_payload") as mock_delayed, \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value={"message_body": "Menú", "options_list": []}) as mock_bot:
            first_payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "hola",
                "ProfileName": "Tester",
            }
            first_response = self.client.post("/webhook/whatsapp", data=first_payload, headers=headers)
            self.assertEqual(first_response.status_code, 200)

            second_payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "consulta",
                "ProfileName": "Tester",
            }
            second_response = self.client.post("/webhook/whatsapp", data=second_payload, headers=headers)

        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(mock_bot.called)
        self.assertEqual(mock_bot.call_args.kwargs["owner_user"].id, self.mock_client_user.id)

    def test_webhook_uses_correct_owner_for_additional_number(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"

        second_user = User(
            name="Cuatro Fincas",
            email="cuatro@example.com",
            rol="empresa",
            tipo_chat="pyme",
            pyme_id=2,
            nombre_empresa="Cuatro Fincas",
        )
        second_user.set_password("otrotest")
        db.session.add(second_user)
        db.session.commit()

        second_number = "+15559876543"
        second_mapping = WhatsappNumero(
            numero_whatsapp=second_number.replace("+", ""),
            user_id=second_user.id,
            is_active=True,
        )
        db.session.add(second_mapping)
        db.session.commit()

        second_session = ChatSessionContext(
            chat_session_id=f"whatsapp_{second_user.id}_{self.test_user_number_str}",
            user_id=second_user.id,
            anon_id=self.test_user_number_str,
            context_data={
                "historial_chat": [],
                "estado_conversacion": "activo",
                "user_id_empresa": second_user.id,
                "telefono_usuario": self.test_user_number_str,
                "canal_origen": "whatsapp",
                "mensajes_previos_llm_formato": [],
                "perfil_confirmado": True,
                "last_welcome_ts": time.time(),
            },
        )
        db.session.add(second_session)
        db.session.commit()

        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch("routes.whatsapp_webhook._send_delayed_payload") as mock_delayed, \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value={"message_body": "Menú", "options_list": []}) as mock_bot:
            first_payload = {
                "To": f"whatsapp:{second_number}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "hola",
                "ProfileName": "Tester",
            }
            first_response = self.client.post("/webhook/whatsapp", data=first_payload, headers=headers)
            self.assertEqual(first_response.status_code, 200)

            second_payload = {
                "To": f"whatsapp:{second_number}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "consulta",
                "ProfileName": "Tester",
            }
            second_response = self.client.post("/webhook/whatsapp", data=second_payload, headers=headers)

        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(mock_bot.called)
        self.assertEqual(mock_bot.call_args.kwargs["owner_user"].id, second_user.id)

    def test_welcome_includes_media_when_configured(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "/static/welcome/sticker.png"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertIn("content_sid", template_kwargs)

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        expected_media = ["https://chatboc.ar/static/welcome/sticker.png"]
        self.assertEqual(sticker_kwargs.get("media_url"), expected_media)
        self.assertNotIn("persistent_action", sticker_kwargs)

        greeting_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("body", greeting_kwargs)

    def test_welcome_payload_resolves_audio_and_existing_image(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "/static/welcome/sticker.png"
        self.app.config["WELCOME_AUDIO_URL"] = "/static/welcome/bienvenida.mp3"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        captured = {}

        def fake_delayed(client, to_number, from_number, payload, delay, app):
            captured["payload"] = payload
            captured["delay"] = delay
            captured["to_number"] = to_number
            captured["from_number"] = from_number
            captured["app"] = app

        from services.pymes import get_or_create_user_by_phone

        known_user = get_or_create_user_by_phone(self.test_user_number_str, self.mock_client_user)
        known_user.name = "Tester"
        db.session.add(known_user)
        db.session.commit()

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=fake_delayed) as mock_delayed, \
             patch("routes.whatsapp_webhook.responder_chatboc") as mock_bot:
            mock_bot.return_value = {
                "message_body": "Menú principal",
                "options_list": [],
                "image_url": "/static/menu/banner.png",
            }

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        mock_delayed.assert_called_once()
        self.assertIn("payload", captured)
        delayed_payload = captured["payload"]
        self.assertIn("audio_url", delayed_payload)
        self.assertEqual(
            delayed_payload["audio_url"],
            "https://chatboc.ar/static/welcome/bienvenida.mp3",
        )
        # Ensure the widget/web payload can reuse the resolved base URL.
        self.assertEqual(delayed_payload.get("_base_url"), "https://chatboc.ar")
        self.assertTrue(
            delayed_payload.get("_request_url_root", "").startswith("http://localhost")
        )

    def test_welcome_payload_without_image_does_not_attach_sticker(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "/static/welcome/sticker.png"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {"message_body": "Menú principal", "options_list": []}

        with patch("routes.whatsapp_webhook._send_delayed_payload") as mock_delayed, \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("image_url", response_payload)

    def test_welcome_payload_matching_sticker_is_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "/static/welcome/sticker.png"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "image_url": "/static/welcome/sticker.png",
        }

        from services.pymes import get_or_create_user_by_phone

        known_user = get_or_create_user_by_phone(self.test_user_number_str, self.mock_client_user)
        known_user.name = "Tester"
        db.session.commit()

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed) as mock_delayed, \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        mock_delayed.assert_called_once()
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("image_url", delayed_payload)

    def test_welcome_payload_matching_sticker_different_domain_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/static/welcome/sticker.webp"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "image_url": "https://cdn.example.net/assets/welcome/STICKER.webp",
        }

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("image_url", delayed_payload)

    def test_welcome_payload_matching_sticker_string_media_url_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "media_url": "https://example.com/sticker.webp",
        }

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("media_url", delayed_payload)

    def test_welcome_payload_matching_sticker_with_http_scheme_is_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "image_url": "http://example.com/sticker.webp",
        }

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("image_url", delayed_payload)

    def test_welcome_payload_matching_sticker_with_query_is_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "image_url": "https://example.com/sticker.webp?updated=123",
        }

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("image_url", delayed_payload)

    def test_welcome_payload_header_matching_sticker_is_removed(self):
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Tester",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self._create_confirmed_session()

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "header": {
                "type": "image",
                "image": {"link": "http://example.com/sticker.webp"},
            },
        }

        captured = {}

        def capture_delayed(**kwargs):
            for key, value in kwargs.items():
                try:
                    captured[key] = copy.deepcopy(value)
                except TypeError:
                    captured[key] = value

        with patch("routes.whatsapp_webhook._send_delayed_payload", side_effect=capture_delayed), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value=response_payload):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        delayed_payload = captured.get("payload", {})
        self.assertNotIn("header", delayed_payload)

    def test_strip_duplicate_media_preserves_header_when_flagged(self):
        sticker_url = "https://example.com/static/welcome/sticker.webp"
        payload = {
            "interactive": {
                "type": "list",
                "header": {"type": "image", "image": {"link": sticker_url}},
                "body": {"text": "Menú"},
                "action": {"sections": []},
            },
            "_preserve_welcome_header": True,
        }

        _strip_duplicate_welcome_media(
            payload,
            sticker_urls=[sticker_url],
            base_url="https://example.com",
        )

        interactive = payload.get("interactive", {})
        self.assertIn("header", interactive)

    def test_welcome_template_failure_still_sends_followups(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        def fail_first(*args, **kwargs):
            call_index = len(self.mock_twilio_create.call_args_list)
            if call_index == 0:
                raise Exception("template failure")
            return MagicMock()

        self.mock_twilio_create.side_effect = fail_first

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        # Even though the template failed, we still attempt the sticker and greeting.
        self.assertGreaterEqual(self.mock_twilio_create.call_count, 3)
        # Second call should correspond to the sticker send.
        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(
            sticker_kwargs.get("media_url"),
            [self.app.config["WELCOME_MEDIA_URL"]],
        )
        greeting_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("body", greeting_kwargs)

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_delayed_payload_upgrades_image_url_to_https(self, mock_build_response, mock_timer):
        self.app.config["APP_BASE_URL"] = "http://chatboc.ar"

        payload = {
            "message_body": "Hola",
            "options_list": [],
            "message_type": "text",
            "image_url": "/static/welcome/sticker.png",
            "_base_url": "http://chatboc.ar",
            "_request_url_root": "http://chatboc.ar",
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Hola"},
            "image_url": "/static/welcome/sticker.png",
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111",
            from_number="whatsapp:+222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertTrue(sent_messages)
        first_call = sent_messages[0]
        self.assertEqual(first_call.get("media_url"), ["https://chatboc.ar/static/welcome/sticker.png"])

    @patch('routes.whatsapp_webhook.threading.Timer')
    @patch('services.response_formatter.build_interactive_response')
    def test_delayed_payload_supports_media_urls(self, mock_build_response, mock_timer):
        self.app.config["APP_BASE_URL"] = "http://api.chatboc.ar"

        payload = {
            "message_body": "Hola",
            "options_list": [],
            "message_type": "text",
            "media_urls": [
                "/static/qr/demo.png",
                "http://cdn.example.com/banner.jpg",
            ],
            "_base_url": "http://api.chatboc.ar",
            "_request_url_root": "http://api.chatboc.ar",
        }

        mock_build_response.return_value = {
            "type": "text",
            "text": {"body": "Hola"},
        }

        class ImmediateTimer:
            def __init__(self, delay, callback):
                self.callback = callback

            def start(self):
                self.callback()

        mock_timer.side_effect = lambda delay, callback: ImmediateTimer(delay, callback)

        sent_messages = []

        def fake_create(**kwargs):
            sent_messages.append(kwargs)
            msg = MagicMock()
            msg.sid = f"SM{len(sent_messages)}"
            return msg

        client = MagicMock()
        client.messages.create.side_effect = fake_create

        _send_delayed_payload(
            client=client,
            to_number="whatsapp:+111",
            from_number="whatsapp:+222",
            payload=payload,
            delay=0,
            app=self.app,
        )

        self.assertTrue(sent_messages)
        media_urls = sent_messages[0].get("media_url")
        self.assertEqual(len(media_urls), 2)
        self.assertIn("https://api.chatboc.ar/static/qr/demo.png", media_urls)
        self.assertIn("https://cdn.example.com/banner.jpg", media_urls)

    def test_welcome_skips_generic_profile_name(self):
        """Generic profile names should trigger a name request."""
        self._set_owner_tipo_chat("municipio")
        self._create_confirmed_session()
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
            "ProfileName": "Vecino/a",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertEqual(json.loads(template_kwargs["content_variables"]).get("1"), "")

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(
            sticker_kwargs.get("media_url"),
            [self.app.config["WELCOME_MEDIA_URL"]],
        )
        self.assertNotIn("body", sticker_kwargs)

        text_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("¿Cómo te llamás?", text_kwargs.get("body"))
        self.assertNotIn("Vecino/a", text_kwargs.get("body"))
        self.assertNotIn("media_url", text_kwargs)

    def test_welcome_asks_for_name_when_unknown(self):
        """When no name is known, the bot should ask for it."""
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(
            sticker_kwargs.get("media_url"),
            [self.app.config["WELCOME_MEDIA_URL"]],
        )
        self.assertNotIn("body", sticker_kwargs)

        text_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("¿Cómo te llamás?", text_kwargs.get("body"))
        self.assertNotIn("media_url", text_kwargs)

        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        self.assertTrue(ctx.context_data.get("awaiting_user_name"))


    def test_welcome_uses_stored_name_with_sticker(self):
        self._set_owner_tipo_chat("municipio")
        self._create_confirmed_session()
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_TEMPLATE_SID"] = "fake_template_sid"
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        context_data = dict(ctx.context_data or {})
        context_data["profile_name"] = "Marcelo"
        context_data[CONTEXTO_MUNICIPIO] = {
            "contacto_usuario": {"nombre": "Marcelo"},
        }
        ctx.context_data = context_data
        db.session.add(ctx)
        db.session.commit()

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertEqual(json.loads(template_kwargs["content_variables"]).get("1"), "Marcelo")

        sticker_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(sticker_kwargs.get("media_url"), [self.app.config["WELCOME_MEDIA_URL"]])

        greeting_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("Marcelo", greeting_kwargs.get("body"))
        self.assertNotIn("Cómo te llamás", greeting_kwargs.get("body"))

    def test_name_reply_sends_personalized_sticker_and_remembers_name(self):
        self._set_owner_tipo_chat("municipio")
        self._create_confirmed_session()
        self.mock_validator.validate.return_value = True
        self.app.config["WELCOME_MEDIA_URL"] = "https://example.com/sticker.webp"

        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        context_data = dict(ctx.context_data or {})
        context_data["awaiting_user_name"] = True
        context_data["_welcome_state"] = {"sticker": {"last_sent_ts": time.time()}}
        ctx.context_data = context_data
        db.session.add(ctx)
        db.session.commit()

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Soy Marcelo",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch("routes.whatsapp_webhook.extract_multiple_contact_details_llm", return_value={"nombre": "Marcelo"}), \
             patch("routes.whatsapp_webhook.responder_chatboc", return_value={"message_body": "Menú", "options_list": []}), \
             patch("routes.whatsapp_webhook._send_delayed_payload") as mock_delayed:
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 2)

        sticker_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertEqual(sticker_kwargs.get("media_url"), [self.app.config["WELCOME_MEDIA_URL"]])

        greeting_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertIn("Marcelo", greeting_kwargs.get("body"))
        mock_delayed.assert_called_once()

        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        self.assertFalse(ctx.context_data.get("awaiting_user_name"))
        self.assertEqual(ctx.context_data.get("profile_name"), "Marcelo")
        municipio_ctx = ctx.context_data.get(CONTEXTO_MUNICIPIO, {})
        self.assertEqual(municipio_ctx.get("contacto_usuario", {}).get("nombre"), "Marcelo")
        self.assertTrue(
            ctx.context_data.get("_welcome_state", {})
            .get("sticker", {})
            .get("personalized_name_sent_ts")
        )

    def test_whatsapp_webhook_invalid_signature(self):
        # Arrange
        self.mock_validator.validate.return_value = False # Simulate invalid signature
        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_invalid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 403) # Expect Forbidden
        self.mock_validator.validate.assert_called_once()
        self.mock_twilio_create.assert_not_called() # Message should not be sent
        self.mock_welcome.assert_not_called()

    def test_numeric_input_ignored_when_waiting_info(self):
        """Ensure numeric shortcuts are disabled when awaiting free text."""
        self._create_confirmed_session()
        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        ctx.context_data["last_options_sent"] = [
            {"id": "menu_principal", "texto": "Menú"},
            {"id": "cancelar", "texto": "Cancelar"},
        ]
        ctx.context_data[CONTEXTO_MUNICIPIO] = {"esperando_info_llm": "ubicacion"}
        db.session.add(ctx)
        db.session.commit()

        self.mock_validator.validate.return_value = True
        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "3",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot:
            mock_bot.return_value = {"message_body": "ok"}
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
            self.assertEqual(response.status_code, 200)
            kwargs = mock_bot.call_args.kwargs
            # The numeric input should remain as text because estamos esperando ubicacion
            self.assertEqual(kwargs["pregunta"], "3")

    def test_numeric_input_ignored_when_waiting_claim_info(self):
        """Numeric shortcuts are disabled when waiting claim-specific data."""
        self._create_confirmed_session()
        session_id = f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ctx = ChatSessionContext.query.filter_by(chat_session_id=session_id).first()
        ctx.context_data["last_options_sent"] = [
            {"id": "menu_principal", "texto": "Menú"},
            {"id": "cancelar", "texto": "Cancelar"},
        ]
        ctx.context_data[CONTEXTO_MUNICIPIO] = {"esperando_info_llm_reclamo": "descripcion"}
        db.session.add(ctx)
        db.session.commit()

        self.mock_validator.validate.return_value = True
        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "2",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot:
            mock_bot.return_value = {"message_body": "ok"}
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
            self.assertEqual(response.status_code, 200)
            kwargs = mock_bot.call_args.kwargs
            self.assertEqual(kwargs["pregunta"], "2")

    def test_whatsapp_webhook_number_not_found_in_db(self):
        # Arrange
        self.mock_validator.validate.return_value = True
        unknown_twilio_number = "+15550000000" # A number not in our mock DB
        payload = {
            "To": f"whatsapp:{unknown_twilio_number}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 404) # Expect Not Found
        self.assertIn("WhatsApp number not configured", response.data.decode())
        self.mock_twilio_create.assert_not_called()
        self.mock_welcome.assert_not_called()

    def test_whatsapp_webhook_number_inactive_in_db(self):
        # Arrange
        self.mock_validator.validate.return_value = True

        # Deactivate the existing mapping
        inactive_mapping = WhatsappNumero.query.filter_by(numero_whatsapp=self.test_whatsapp_number_str).first()
        if inactive_mapping:
            inactive_mapping.is_active = False
            db.session.add(inactive_mapping)
            db.session.commit()

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}", # Number is now inactive
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 404) # Expect Not Found (as if not configured)
        self.assertIn("WhatsApp number not configured", response.data.decode())
        self.mock_twilio_create.assert_not_called()
        self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_pdf_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-pdf-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_pdf_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 1
            mock_adjunto.url = 'http://fake.storage/test.pdf'
            mock_adjunto.mime = 'application/pdf'
            mock_adjunto.nombre_original = 'test.pdf'

            def _assert_owner(file_storage, user_id=None, session_id=None):
                owner_user = getattr(g, "owner_user", None)
                self.assertIsNotNone(owner_user)
                self.assertEqual(getattr(owner_user, "id", None), self.mock_client_user.id)
                return mock_adjunto

            mock_create_attachment.side_effect = _assert_owner
            mock_classifier.return_value = {"categoria_sugerida": "documentacion"}

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.pdf",
                "MediaContentType0": "application/pdf"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "application/pdf")
            self.assertIn("datos_interpretados_archivo", kwargs)
            self.assertEqual(
                kwargs["datos_interpretados_archivo"], {"categoria_sugerida": "documentacion"}
            )

            self.mock_twilio_create.assert_called_once()
            _, kwargs_twilio = self.mock_twilio_create.call_args
            self.assertEqual(kwargs_twilio["from_"], f"whatsapp:{self.test_whatsapp_number_str}")
            self.assertEqual(kwargs_twilio["to"], f"whatsapp:{self.test_user_number_str}")
            self.assertTrue(kwargs_twilio["body"].startswith("Ok"))
            self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_text_response_with_image_sends_media(self, mock_bot):
        self._create_confirmed_session()
        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_image"
        self.mock_twilio_create.return_value = mock_twilio_message

        mock_bot.return_value = {
            "message_body": "Hola",
            "options_list": [],
            "message_type": "text",
            "image_url": "http://example.com/promo.jpg"
        }

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "consulta"
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        def instant_timer(delay, func):
            class Dummy:
                daemon = True
                def __init__(self, delay, func):
                    func()
                def start(self):
                    pass
            return Dummy(delay, func)

        with patch('routes.whatsapp_webhook.threading.Timer', side_effect=instant_timer):
            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        self.mock_twilio_create.assert_called()

        found = False
        for call in self.mock_twilio_create.call_args_list:
            kwargs_twilio = call.kwargs
            if 'media_url' in kwargs_twilio:
                self.assertEqual(kwargs_twilio['media_url'][0], 'http://example.com/promo.jpg')
                found = True
                break
        self.assertTrue(found, "Expected media_url call not found")

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_docx_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-docx-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_docx_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 2
            mock_adjunto.url = 'http://fake.storage/test.docx'
            mock_adjunto.mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            mock_adjunto.nombre_original = 'test.docx'
            mock_create_attachment.return_value = mock_adjunto
            mock_classifier.return_value = {"categoria_sugerida": "documentacion"}

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.docx",
                "MediaContentType0": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(
                kwargs["uploaded_file_info"]["mime_type"],
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
            self.assertIn("datos_interpretados_archivo", kwargs)
            self.assertEqual(
                kwargs["datos_interpretados_archivo"], {"categoria_sugerida": "documentacion"}
            )

            self.mock_twilio_create.assert_called_once()
            _, kwargs_twilio = self.mock_twilio_create.call_args
            self.assertEqual(kwargs_twilio["from_"], f"whatsapp:{self.test_whatsapp_number_str}")
            self.assertEqual(kwargs_twilio["to"], f"whatsapp:{self.test_user_number_str}")
            self.assertTrue(kwargs_twilio["body"].startswith("Ok"))
            self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_image_attachment(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-image-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_image_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 3
            mock_adjunto.url = 'http://fake.storage/test.jpg'
            mock_adjunto.mime = 'image/jpeg'
            mock_adjunto.nombre_original = 'test.jpg'
            mock_create_attachment.return_value = mock_adjunto
            mock_classifier.return_value = {"categoria_sugerida": "reclamo"}

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/test.jpg",
                "MediaContentType0": "image/jpeg"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "image/jpeg")
            self.assertIn("datos_interpretados_archivo", kwargs)
            self.assertEqual(
                kwargs["datos_interpretados_archivo"], {"categoria_sugerida": "reclamo"}
            )

            self.mock_twilio_create.assert_called_once()
            _, kwargs_twilio = self.mock_twilio_create.call_args
            self.assertEqual(kwargs_twilio["from_"], f"whatsapp:{self.test_whatsapp_number_str}")
            self.assertEqual(kwargs_twilio["to"], f"whatsapp:{self.test_user_number_str}")
            self.assertTrue(kwargs_twilio["body"].startswith("Ok"))
        self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_numeric_option_is_mapped_to_text(self, mock_bot):
        self.mock_validator.validate.return_value = True
        self._create_confirmed_session()

        # preset last options in context
        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ).first()
        session.context_data["last_options_sent"] = [
            {"texto": "Sí, es correcto"},
            {"texto": "No, quiero editar"},
        ]
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "context_data")
        db.session.commit()

        mock_bot.return_value = {"message_body": "ok"}

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "1",
        }
        headers = {"X-Twilio-Signature": "sig"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_called_once()
        _, kwargs = mock_bot.call_args
        self.assertEqual(kwargs["pregunta"], "Sí, es correcto")

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_numeric_option_uses_category_name(self, mock_bot):
        self.mock_validator.validate.return_value = True
        self._create_confirmed_session()

        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ).first()
        session.context_data["last_options_sent"] = [
            {"texto": "*Volver al inicio*", "id_accion": "0", "category_name": "Volver al inicio"},
            {"texto": "💡 *Luminaria*", "id_accion": "1", "category_name": "Luminaria"},
            {"texto": "💧 *Pérdida de agua*", "id_accion": "5", "category_name": "Pérdida de agua"},
        ]
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(session, "context_data")
        db.session.commit()

        mock_bot.return_value = {"message_body": "ok"}

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "3",
        }
        headers = {"X-Twilio-Signature": "sig"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(response.status_code, 200)
        mock_bot.assert_called_once()
        _, kwargs = mock_bot.call_args
        self.assertEqual(kwargs["pregunta"], "Pérdida de agua")

    @patch('routes.whatsapp_webhook.requests.get')
    def test_image_attachment_skips_analysis_when_reclamo_active(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-image-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_image_skip"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.empresa_id_for_test}_{self.test_user_number_str}"
        ).first()
        session.context_data.setdefault(CONTEXTO_MUNICIPIO, {})['reclamo_flow_v2'] = {
            'state': 'ESPERANDO_FOTO',
            'datos_reclamo': {}
        }
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(session, "context_data")
        db.session.commit()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 33
            mock_adjunto.url = 'http://fake.storage/skip.jpg'
            mock_adjunto.mime = 'image/jpeg'
            mock_adjunto.nombre_original = 'skip.jpg'
            mock_create_attachment.return_value = mock_adjunto

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Archivo",
                "MediaUrl0": "http://example.com/skip.jpg",
                "MediaContentType0": "image/jpeg"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_classifier.assert_not_called()
            mock_bot.assert_called_once()
            kwargs = mock_bot.call_args.kwargs
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "image/jpeg")
            self.assertTrue(kwargs.get("es_foto"))
            self.assertNotIn("datos_interpretados_archivo", kwargs)

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_audio_attachment_skips_classification(self, mock_requests_get):
        # Mock the download response
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-audio-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_audio_test"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('services.audio_transcription_service.transcribe_audio_from_url') as mock_transcribe, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 4
            mock_adjunto.url = 'http://fake.storage/test.ogg'
            mock_adjunto.mime = 'audio/ogg'
            mock_adjunto.nombre_original = 'test.ogg'
            mock_create_attachment.return_value = mock_adjunto
            mock_transcribe.return_value = None

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "Audio",
                "MediaUrl0": "http://example.com/test.ogg",
                "MediaContentType0": "audio/ogg"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data.decode(), "OK")

            mock_bot.assert_called_once()
            _, kwargs = mock_bot.call_args
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["mime_type"], "audio/ogg")
            self.assertNotIn("datos_interpretados_archivo", kwargs)
            mock_classifier.assert_not_called()

            self.mock_twilio_create.assert_called_once()
            _, kwargs_twilio = self.mock_twilio_create.call_args
            self.assertEqual(kwargs_twilio["from_"], f"whatsapp:{self.test_whatsapp_number_str}")
            self.assertEqual(kwargs_twilio["to"], f"whatsapp:{self.test_user_number_str}")
            self.assertTrue(kwargs_twilio["body"].startswith("Ok"))
            self.mock_welcome.assert_not_called()

    @patch('routes.whatsapp_webhook.requests.get')
    def test_whatsapp_webhook_audio_attachment_transcribes_text(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.content = b'fake-audio-content'
        mock_requests_get.return_value = mock_response

        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SM_audio_transcribed"
        self.mock_twilio_create.return_value = mock_twilio_message

        self._create_confirmed_session()

        with patch('routes.whatsapp_webhook.responder_chatboc') as mock_bot, \
             patch('routes.whatsapp_webhook.create_attachment_with_thumbnail') as mock_create_attachment, \
             patch('services.audio_transcription_service.transcribe_audio_from_url') as mock_transcribe, \
             patch('routes.whatsapp_webhook.clasificar_adjunto_whatsapp') as mock_classifier:
            mock_bot.return_value = {"message_body": "Ok"}
            mock_adjunto = MagicMock()
            mock_adjunto.id = 5
            mock_adjunto.url = 'http://fake.storage/test.ogg'
            mock_adjunto.mime = 'audio/ogg'
            mock_adjunto.nombre_original = 'test.ogg'
            mock_create_attachment.return_value = mock_adjunto
            mock_transcribe.return_value = "hola que tal"

            payload = {
                "To": f"whatsapp:{self.test_whatsapp_number_str}",
                "From": f"whatsapp:{self.test_user_number_str}",
                "Body": "",
                "MediaUrl0": "http://example.com/test.ogg",
                "MediaContentType0": "audio/ogg"
            }
            headers = {"X-Twilio-Signature": "dummy_signature_valid"}

            response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

            self.assertEqual(response.status_code, 200)
            mock_bot.assert_called_once()
            kwargs = mock_bot.call_args.kwargs
            self.assertEqual(kwargs["pregunta"], "hola que tal")
            self.assertIn("uploaded_file_info", kwargs)
            self.assertEqual(kwargs["uploaded_file_info"]["transcribed_text"], "hola que tal")
            mock_classifier.assert_not_called()
            mock_transcribe.assert_called_once()

            self.mock_twilio_create.assert_called_once()
            _, kwargs_twilio = self.mock_twilio_create.call_args
            self.assertEqual(kwargs_twilio["from_"], f"whatsapp:{self.test_whatsapp_number_str}")
            self.assertEqual(kwargs_twilio["to"], f"whatsapp:{self.test_user_number_str}")
            self.assertTrue(kwargs_twilio["body"].startswith("Ok"))
            self.mock_welcome.assert_not_called()

    def test_whatsapp_payload_sends_pre_messages_before_main_message(self):
        self._set_owner_tipo_chat("municipio")
        self.mock_validator.validate.return_value = True
        self._create_confirmed_session()

        self.app.config["WELCOME_TEMPLATE_SID"] = None
        self.app.config["WELCOME_MEDIA_URL"] = None
        self.app.config["WELCOME_AUDIO_URL"] = None

        headers = {"X-Twilio-Signature": "dummy_signature_valid"}
        incoming_payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "encuestas",
        }

        response_payload = {
            "message_body": "Menú principal",
            "options_list": [],
            "message_type": "text",
            "_twilio_pre_messages": [
                {
                    "channels": ["whatsapp"],
                    "content_sid": "HXbanner",
                    "content_variables": {"1": "Junín"},
                },
                {
                    "channels": ["whatsapp"],
                    "body": "Participá",
                    "media_urls": ["/static/encuestas/banner.png"],
                },
            ],
        }

        with patch(
            "routes.whatsapp_webhook.responder_chatboc", return_value=response_payload
        ):
            response = self.client.post(
                "/webhook/whatsapp", data=incoming_payload, headers=headers
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 3)

        template_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        self.assertEqual(template_kwargs.get("content_sid"), "HXbanner")
        self.assertEqual(
            json.loads(template_kwargs.get("content_variables", "{}")), {"1": "Junín"}
        )

        media_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertEqual(media_kwargs.get("body"), "Participá")
        media_urls = media_kwargs.get("media_url")
        self.assertIsInstance(media_urls, list)
        self.assertTrue(media_urls)
        self.assertTrue(media_urls[0].endswith("/static/encuestas/banner.png"))

        main_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertTrue(
            (main_kwargs.get("body") or "").startswith("Menú principal"),
        )


if __name__ == "__main__":
    unittest.main()
