import unittest
from unittest.mock import patch, MagicMock
import json
import sys
import os

# Add project root to the Python path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from services.municipios import responder_municipio, CONTEXTO_MUNICIPIO, ConversationState
from tests.mocks import MockGeminiResponse

class TestSimulatedClaimFlow(unittest.TestCase):

    def setUp(self):
        self.maxDiff = None
        # Mock user and context objects that would be created in a real request
        self.owner_user = MagicMock()
        self.owner_user.id = 1
        self.owner_user.nombre_empresa = "Municipalidad de Junin"
        self.owner_user.rubro.nombre = "municipio"

        self.viewer_user = None # Simulate anonymous user

        self.chat_db_context = MagicMock()
        self.chat_db_context.context_data = {}

    @patch('services.gemini_bridge.llamar_gemini')
    def test_full_claim_flow(self, mock_llamar_gemini):
        # Step 1: User says "Hola"
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "¡Hola! Soy tu asistente virtual. ¿Cómo puedo ayudarte hoy?",
            "accion_backend": "saludar",
            "datos_estructura": {"target": "general"},
            "pedir_info": None,
            "botones": [{"texto": "Hacer un reclamo"}, {"texto": "Consultar trámite"}]
        }

        response = responder_municipio(
            pregunta_original="Hola",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id"
        )

        self.assertEqual(response['message_body'], "¡Hola! Soy tu asistente virtual. ¿Cómo puedo ayudarte hoy?")

        # Step 2: User wants to make a claim
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?",
            "accion_backend": "iniciar_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Limpieza/Basura",
                "descripcion": "Contenedor de basura rebalsado"
            },
            "pedir_info": "ubicacion",
            "botones": []
        }

        response = responder_municipio(
            pregunta_original="Quiero hacer un reclamo por un contenedor de basura que está rebalsado.",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id"
        )

        self.assertEqual(response['message_body'], "Entendido. Para registrar tu reclamo por el contenedor de basura, ¿podrías decirme la dirección exacta donde se encuentra?")
        self.assertEqual(self.chat_db_context.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'], ConversationState.ESPERANDO_DIRECCION_RECLAMO.name)

        # Step 3: User provides location
        mock_llamar_gemini.return_value = {
            "respuesta_usuario": "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?",
            "accion_backend": "crear_reclamo",
            "datos_estructura": {
                "target": "municipio",
                "categoria": "Limpieza/Basura",
                "descripcion": "Contenedor de basura rebalsado",
                "ubicacion": "Don Bosco 55, Junín, Mendoza"
            },
            "pedir_info": "nombre_y_telefono",
            "botones": []
        }

        response = responder_municipio(
            pregunta_original="don bosco 55 junin mendoza",
            owner_user=self.owner_user,
            viewer_user=self.viewer_user,
            chat_db_context=self.chat_db_context,
            anon_id="test_anon_id"
        )

        self.assertEqual(response['message_body'], "Perfecto. Registré tu reclamo por un contenedor lleno en Don Bosco 55, Junín, Mendoza. Para finalizar, ¿me podrías dar tu nombre completo y un teléfono?")
        self.assertEqual(self.chat_db_context.context_data[CONTEXTO_MUNICIPIO]['estado_conversacion'], ConversationState.ESPERANDO_NOMBRE_VECINO.name)

        # Step 4: User provides personal data
        with patch('services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket') as mock_crear_ticket:
            mock_ticket = MagicMock()
            mock_ticket.nro_ticket = "M-12345"
            mock_crear_ticket.return_value = mock_ticket

            mock_llamar_gemini.return_value = {
                "respuesta_usuario": "¡Listo! Tu reclamo por la luminaria en Mitre y Belgrano fue registrado con el número M-12345. Te avisaremos sobre cualquier novedad. ¿Necesitas algo más?",
                "accion_backend": "crear_reclamo",
                "datos_estructura": {
                    "target": "municipio",
                    "categoria": "Limpieza/Basura",
                    "descripcion": "Contenedor de basura rebalsado",
                    "ubicacion": "Don Bosco 55, Junín, Mendoza",
                    "nombre_usuario_detectado": "Juan Perez",
                    "telefono_detectado": "2611234567"
                },
                "pedir_info": None,
                "botones": [
                    {"texto": "Consultar otro reclamo", "id_accion": "consultar_estado_ticket"},
                    {"texto": "Hacer otro reclamo", "id_accion": "iniciar_reclamo"}
                ]
            }

            response = responder_municipio(
                pregunta_original="Juan Perez 2611234567",
                owner_user=self.owner_user,
                viewer_user=self.viewer_user,
                chat_db_context=self.chat_db_context,
                anon_id="test_anon_id"
            )

            self.assertIn("Se ha generado el ticket de reclamo con el número", response['message_body'])
            self.assertIsNone(self.chat_db_context.context_data[CONTEXTO_MUNICIPIO].get('estado_conversacion'))

if __name__ == '__main__':
    unittest.main()
