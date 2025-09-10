import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Añadir el directorio raíz del proyecto al sys.path
project_root_whatsapp = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root_whatsapp not in sys.path:
    sys.path.insert(0, project_root_whatsapp)

from app import create_app, db
from config import Config
from models import User, Rubro, WhatsappNumero, ChatSessionContext
from services.municipio_responder import CONTEXTO_MUNICIPIO
# Moved model imports after app and config to ensure they are found via sys.path
# and to avoid potential issues if models.py itself tries to import app-context related things early.
# However, for direct use in tests, they are typically at the top. Let's try keeping them here.
import models
# JSON import is no longer needed
# import json

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:' # Use in-memory SQLite for tests
    WTF_CSRF_ENABLED = False
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

        self.welcome_patch = patch('routes.whatsapp_webhook.enviar_bienvenida_whatsapp', MagicMock())
        self.mock_welcome = self.welcome_patch.start()

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

    def test_whatsapp_webhook_valid_request(self):
        # Arrange
        self.mock_validator.validate.return_value = True

        mock_twilio_message = MagicMock()
        mock_twilio_message.sid = "SMxxxxxxxxxxxxxxxxxxxxxxxxxxxxx_test_sid"
        self.mock_twilio_create.return_value = mock_twilio_message

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "Hello Test"
        }
        headers = { "X-Twilio-Signature": "dummy_signature_valid" }

        # Act
        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Assert
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.decode(), "OK")
        self.mock_validator.validate.assert_called_once()

        # The bot should now derive to human, so we don't expect an echo.
        # We just check that the webhook returns OK and that the bot was called.
        # The response to the user is handled by the bot logic, which is mocked here.
        # In a real scenario, the bot would send a message like "Connecting you to an agent..."
        # and the test for that would be in the bot logic tests, not the webhook test.
        self.mock_twilio_create.assert_called_once()
        self.mock_welcome.assert_called_once_with(
            self.test_user_number_str,
            "Vecino/a"
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
            mock_create_attachment.return_value = mock_adjunto
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
            "Body": "hola"
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        response = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.mock_twilio_create.call_count, 2)
        first_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        second_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        self.assertIn('body', first_kwargs)
        self.assertNotIn('media_url', first_kwargs)
        self.assertIn('media_url', second_kwargs)
        self.assertEqual(second_kwargs['media_url'][0], 'http://example.com/promo.jpg')

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

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_state_hint_persists_in_session(self, mock_bot):
        """Ensure next_state_hint from bot updates the stored conversation state."""
        self._create_confirmed_session()
        session = ChatSessionContext.query.first()
        session.context_data[CONTEXTO_MUNICIPIO] = {"estado_conversacion": "ESPERANDO_DIRECCION_RECLAMO"}
        db.session.commit()

        mock_bot.return_value = {"message_body": "Ok", "next_state_hint": "ESPERANDO_DATOS_CONTACTO"}

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        updated = ChatSessionContext.query.first().context_data[CONTEXTO_MUNICIPIO]["estado_conversacion"]
        self.assertEqual(updated, "ESPERANDO_DATOS_CONTACTO")

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_state_hint_survives_context_merge(self, mock_bot):
        """State hint should persist even when contexto_municipio_v2 is updated."""
        self._create_confirmed_session()
        session = ChatSessionContext.query.first()
        session.context_data[CONTEXTO_MUNICIPIO] = {"estado_conversacion": "ESPERANDO_DIRECCION_RECLAMO"}
        db.session.commit()

        mock_bot.return_value = {
            "message_body": "Ok",
            "next_state_hint": "ESPERANDO_DATOS_CONTACTO",
            "contexto_actualizado": {CONTEXTO_MUNICIPIO: {"ultimo_menu": "principal"}},
        }

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        stored_ctx = ChatSessionContext.query.first().context_data[CONTEXTO_MUNICIPIO]
        self.assertEqual(stored_ctx["estado_conversacion"], "ESPERANDO_DATOS_CONTACTO")
        self.assertEqual(stored_ctx["ultimo_menu"], "principal")

    @patch('routes.whatsapp_webhook.responder_chatboc')
    def test_interactive_sends_plain_text_first(self, mock_bot):
        """Interactive payloads should always send a text message fallback before the interactive one."""
        self._create_confirmed_session()
        mock_bot.return_value = {
            "message_body": "Hola",
            "options_list": [{"texto": "Opción 1", "id": "opt1"}],
            "message_type": "interactive_buttons",
            "image_url": "http://example.com/promo.jpg",
        }

        payload = {
            "To": f"whatsapp:{self.test_whatsapp_number_str}",
            "From": f"whatsapp:{self.test_user_number_str}",
            "Body": "hola",
        }
        headers = {"X-Twilio-Signature": "dummy_signature_valid"}

        with patch.dict(os.environ, {"WHATSAPP_ALLOW_INTERACTIVE": "true"}), \
             patch('services.response_formatter.WHATSAPP_ALLOW_INTERACTIVE', True):
            self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        # Text first, then image, then interactive payload
        self.assertGreaterEqual(self.mock_twilio_create.call_count, 3)
        first_kwargs = self.mock_twilio_create.call_args_list[0].kwargs
        second_kwargs = self.mock_twilio_create.call_args_list[1].kwargs
        third_kwargs = self.mock_twilio_create.call_args_list[2].kwargs
        self.assertIn("body", first_kwargs)
        self.assertNotIn("media_url", first_kwargs)
        self.assertIn("media_url", second_kwargs)
        self.assertIn("persistent_action", third_kwargs)

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

if __name__ == "__main__":
    unittest.main()

def test_deep_merge_preserves_nested_fields():
    from routes.whatsapp_webhook import deep_merge_dict

    original = {"contexto_municipio_v2": {"estado_conversacion": "A"}, "other": 1}
    update = {"contexto_municipio_v2": {"last_options_sent": [1]}, "other": 2}

    merged = deep_merge_dict(original, update)

    assert merged["contexto_municipio_v2"]["estado_conversacion"] == "A"
    assert merged["contexto_municipio_v2"]["last_options_sent"] == [1]
    assert merged["other"] == 2
