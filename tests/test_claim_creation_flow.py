import unittest
from unittest.mock import patch, MagicMock
import os
import sys
import pytest

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.municipio_responder import responder_municipio, CONTEXTO_MUNICIPIO, ConversationState
from models import User, Rubro, ChatSessionContext as ChatSession, db
from config import TestConfig
from app import create_app

@pytest.mark.legacy
@patch('services.google_text_to_speech.TextToSpeechService')
class TestClaimCreationFlow(unittest.TestCase):

    def setUp(self):
        """Configura un entorno de prueba básico."""
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.owner_user = User(id=1, name="Municipio Test", email="municipio@test.com", password_hash="test", municipio_id=1)
        self.viewer_user = User(id=2, name="Vecino Test", email="vecino@test.com", password_hash="test")
        self.rubro_obj = Rubro(id=1, nombre="Municipalidad", clave="municipalidad")
        self.chat_session = ChatSession(chat_session_id="test_session")
        self.chat_session.context_data = {CONTEXTO_MUNICIPIO: {}}
        db.session.add_all([self.owner_user, self.viewer_user, self.rubro_obj, self.chat_session])
        db.session.commit()

        # Helper to call responder_municipio consistently
        self.responder_municipio_test = lambda pregunta_original: responder_municipio(
            pregunta_original=pregunta_original,
            owner_user=self.owner_user,
            rubro_obj=self.rubro_obj,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_session,
            channel="test"
        )


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.google_text_to_speech.TextToSpeechService')
    @patch('services.llm_utils.llamar_llm_para_json_estructurado')
    def test_full_claim_creation_flow(self, mock_llamar_gemini, mock_tts_service, mock_class_tts_service):
        """
        Simula un flujo completo de creación de reclamos, verificando que el contexto se mantiene
        y que la información se recopila correctamente a través de varios mensajes.
        """
        with self.app.app_context():
            # 1. El usuario inicia el reclamo
            mock_llamar_gemini.return_value = {
                "message_body": "Para crear tu reclamo por Alumbrado Público, decime la dirección.",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {"target": "municipio", "categoria": "Alumbrado Público", "descripcion": "Quiero arreglar una luz"},
                "pedir_info": "ubicacion"
            }

            respuesta = self.responder_municipio_test(pregunta_original="Quiero arreglar una luz")

            self.assertIn("Para crear tu reclamo por Alumbrado Público, decime la dirección.", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio['estado_conversacion'], 'ESPERANDO_INFO_RECLAMO_LLM')
            self.assertEqual(contexto_municipio['esperando_info_llm_reclamo'], "ubicacion")
            db.session.commit()

            # 2. El usuario proporciona la dirección
            mock_llamar_gemini.return_value = {
                "message_body": "Gracias. Ahora, ¿cuál es tu nombre completo?",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {"ubicacion": "Calle Falsa 123"},
                "pedir_info": "nombre_completo"
            }

            respuesta = self.responder_municipio_test(pregunta_original="Es en Calle Falsa 123")

            self.assertIn("¿cuál es tu nombre completo?", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio['esperando_info_llm_reclamo'], "nombre_completo")
            self.assertEqual(contexto_municipio['datos_parciales_llm_reclamo'].get('ubicacion'), "Calle Falsa 123")
            self.assertEqual(contexto_municipio['datos_parciales_llm_reclamo'].get('categoria'), "Alumbrado Público")
            db.session.commit()

            # 3. El usuario proporciona el nombre y se crea el ticket
            with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket, \
                 patch('services.actions.municipio_actions.enviar_notificacion_whatsapp_con_plantilla') as mock_whatsapp, \
                 patch('services.actions.municipio_actions.enviar_notificacion_sms') as mock_sms:

                mock_ticket = MagicMock()
                mock_ticket.id = 55
                mock_ticket.nro_ticket = "12345"
                mock_crear_ticket.return_value = mock_ticket

                # El LLM ahora también extrae el email y teléfono que faltan del perfil del usuario
                mock_llamar_gemini.return_value = {
                    "accion_backend": "crear_reclamo",
                    "datos_estructura": {
                        "nombre_usuario_detectado": "Juan Perez",
                        "telefono_detectado": self.viewer_user.telefono,
                        "email_detectado": "vecino@test.com" # Simula un email detectado
                    },
                    "pedir_info": None
                }

                respuesta = self.responder_municipio_test(pregunta_original="Soy Juan Perez")

            self.assertIn("¡Tu reclamo fue generado con éxito, Juan Perez!", respuesta['message_body'])
            self.assertIn("N° de Ticket: M-12345", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio.get('estado_conversacion'), 'CONVERSACION_GENERAL_LLM')
            mock_crear_ticket.assert_called_once()
            # Verifica que el email del viewer_user (que no tenía) se haya actualizado
            self.assertEqual(self.viewer_user.email, "vecino@test.com")

    @patch('services.google_text_to_speech.TextToSpeechService')
    @patch('services.llm_utils.llamar_llm_para_json_estructurado')
    def test_claim_creation_with_google_maps_link(self, mock_llamar_gemini, mock_tts_service, mock_class_tts_service):
        """
        Verifica que el bot puede extraer una dirección de un link de Google Maps.
        """
        with self.app.app_context():
            # El usuario envía un link de Google Maps
            mock_llamar_gemini.return_value = {
                "message_body": "Gracias por la dirección. ¿Podrías describir el problema?",
                "accion_backend": "iniciar_reclamo",
                "datos_estructura": {"target": "municipio", "ubicacion": "Villegas 900, M5584, San Martín, Mendoza, AR"},
                "pedir_info": "descripcion"
            }

            respuesta = responder_municipio(
                pregunta_original="https://maps.google.com/maps/search/Hospedaje%20Finca%20La%20Siciliana/@-33.03129332,-68.50156402,17z?hl=es Villegas 900, M5584, AR",
                owner_user=self.owner_user,
                rubro_obj=self.rubro_obj,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_session
            )

            self.assertIn("Gracias por la dirección. ¿Podrías describir el problema?", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio['datos_parciales_llm_reclamo']['ubicacion'], "Villegas 900, M5584, San Martín, Mendoza, AR")

if __name__ == '__main__':
    unittest.main()
