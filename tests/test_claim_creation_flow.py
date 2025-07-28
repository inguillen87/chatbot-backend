import unittest
from unittest.mock import patch, MagicMock
import os
import sys

# Asegúrate de que el directorio raíz del proyecto esté en el sys.path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.municipios import responder_municipio, CONTEXTO_MUNICIPIO, ConversationState
from models import User, Rubro, ChatSessionContext as ChatSession, db
from config import TestConfig
from app import create_app

class TestClaimCreationFlow(unittest.TestCase):

    def setUp(self):
        """Configura un entorno de prueba básico."""
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.owner_user = User(id=1, name="Municipio Test", email="municipio@test.com", password_hash="test")
        self.viewer_user = User(id=2, name="Vecino Test", email="vecino@test.com", password_hash="test")
        self.rubro_obj = Rubro(id=1, nombre="Municipalidad", clave="municipalidad")
        self.chat_session = ChatSession(chat_session_id="test_session")
        self.chat_session.context_data = {CONTEXTO_MUNICIPIO: {}}
        db.session.add_all([self.owner_user, self.viewer_user, self.rubro_obj, self.chat_session])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.municipios.llamar_gemini')
    @patch('services.municipios.accion_crear_reclamo_municipio')
    def test_full_claim_creation_flow(self, mock_accion_crear_reclamo, mock_llamar_gemini):
        """
        Simula un flujo completo de creación de reclamos, verificando que el contexto se mantiene
        y que la información se recopila correctamente a través de varios mensajes.
        """
        with self.app.app_context():
            # 1. El usuario inicia el reclamo
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "Para crear tu reclamo, decime la dirección.",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"target": "municipio", "categoria": "Alumbrado Público"},
                "pedir_info": "ubicacion"
            }

            respuesta = responder_municipio(
                pregunta_original="Quiero arreglar una luz",
                owner_user=self.owner_user,
                rubro_obj=self.rubro_obj,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_session
            )

            self.assertIn("decime la dirección", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio['estado_conversacion'], ConversationState.ESPERANDO_INFO_RECLAMO_LLM.name)
            self.assertEqual(contexto_municipio['esperando_info_llm_reclamo'], "ubicacion")

            # 2. El usuario proporciona la dirección
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "Gracias. Ahora, ¿cuál es tu nombre completo?",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"ubicacion": "Calle Falsa 123"},
                "pedir_info": "nombre_completo"
            }

            respuesta = responder_municipio(
                pregunta_original="Es en Calle Falsa 123",
                owner_user=self.owner_user,
                rubro_obj=self.rubro_obj,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_session
            )

            self.assertIn("¿cuál es tu nombre completo?", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio['esperando_info_llm_reclamo'], "nombre_completo")
            self.assertEqual(contexto_municipio['datos_parciales_llm_reclamo']['ubicacion'], "Calle Falsa 123")
            self.assertEqual(contexto_municipio['datos_parciales_llm_reclamo']['categoria'], "Alumbrado Público")
            self.assertEqual(contexto_municipio['esperando_info_llm_reclamo'], "nombre_completo")

            # 3. El usuario proporciona el nombre y se crea el ticket
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "Reclamo creado.",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {"nombre_usuario_detectado": "Juan Perez"},
                "pedir_info": None  # No se pide más información
            }
            mock_accion_crear_reclamo.return_value = {
                "success": True,
                "ticket_id": "12345",
                "message_to_user": "Se ha generado el ticket de reclamo N° 12345."
            }

            respuesta = responder_municipio(
                pregunta_original="Soy Juan Perez",
                owner_user=self.owner_user,
                rubro_obj=self.rubro_obj,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_session
            )

            # Verifica que se llamó a la acción de crear reclamo con todos los datos
            mock_accion_crear_reclamo.assert_called_once()
            datos_enviados = mock_accion_crear_reclamo.call_args[0][0]
            self.assertEqual(datos_enviados['categoria'], "Alumbrado Público")
            self.assertEqual(datos_enviados['ubicacion'], "Calle Falsa 123")
            self.assertEqual(datos_enviados['nombre_usuario_detectado'], "Juan Perez")

            # Verifica la respuesta final al usuario
            self.assertIn("ticket de reclamo N° 12345", respuesta['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertEqual(contexto_municipio.get('estado_conversacion'), ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name)

            # 4. El usuario confirma el ticket
            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "¡Gracias! Tu reclamo ha sido confirmado.",
                "accion_backend": "confirmar_reclamo",
                "datos_estructura": {},
                "pedir_info": None
            }

            respuesta_confirmacion = responder_municipio(
                pregunta_original="Sí",
                owner_user=self.owner_user,
                rubro_obj=self.rubro_obj,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_session
            )

            self.assertIn("reclamo ha sido confirmado", respuesta_confirmacion['message_body'])
            contexto_municipio = self.chat_session.context_data[CONTEXTO_MUNICIPIO]
            self.assertIsNone(contexto_municipio.get('estado_conversacion'))

if __name__ == '__main__':
    unittest.main()
