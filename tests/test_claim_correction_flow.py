import unittest
from unittest.mock import patch, MagicMock

# Add project root to the Python path
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app
from extensions import db
from models import User, ChatSessionContext
from services.municipio_responder import handle_llm_interaction, ConversationState, CONTEXTO_MUNICIPIO

class TestClaimCorrectionLogic(unittest.TestCase):

    def setUp(self):
        self.app = create_app('config.TestConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        # Create a dummy user for context if needed
        self.user = User(
            email="test@example.com",
            name="Test User",
            telefono="+1234567890",
            password_hash="dummy_hash", # Add a dummy hash to satisfy NOT NULL constraint
            token="dummy_token"
        )
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.municipio_responder.llamar_gemini')
    @patch('services.municipio_responder.extract_multiple_contact_details_llm')
    def test_correction_in_confirmation_state_merges_data(self, mock_extract_details, mock_llamar_gemini):
        """
        Tests that when a user provides free-text correction in the confirmation state,
        the new data is correctly merged with the existing data.
        """
        # Prevent the main LLM call from executing and interfering.
        # It needs a non-empty body to avoid the None check.
        mock_llamar_gemini.return_value = {"message_body": "mock"}

        # 1. Setup the initial context
        initial_claim_data = {
            'categoria': 'Luminaria',
            'ubicacion': 'bousquet isidoro 5500'
        }
        contexto_municipio_actual = {
            'estado_conversacion': ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name,
            'datos_parciales_llm_reclamo': initial_claim_data,
            'historial_llm_reclamo': []
        }

        # Mock the return value of the contact extraction helper
        mock_extract_details.return_value = {
            "nombre_usuario_detectado": "Marcelo Guillen",
            "email_detectado": "guillen.marce@gmail.com"
        }

        # 2. The user sends their correction
        user_input = "mi nombre es Marcelo Guillen y mi mail es guillen.marce@gmail.com"

        full_context = { "viewer_user_obj": self.user }

        # 3. Call the function under test
        response_dict, updated_context = handle_llm_interaction(
            pregunta_str=user_input, context=full_context, viewer_user=self.user,
            owner_user=None, chat_db_context=None, contexto_municipio_actual=contexto_municipio_actual
        )

        # 4. Assert the results
        self.assertIn("he actualizado tus datos", response_dict['message_body'].lower())
        self.assertEqual(updated_context.get('estado_conversacion'), ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name)
        final_data = updated_context.get('datos_parciales_llm_reclamo', {})
        self.assertEqual(final_data.get('categoria'), 'Luminaria')
        self.assertEqual(final_data.get('ubicacion'), 'bousquet isidoro 5500')
        self.assertEqual(final_data.get('nombre_usuario_detectado'), 'Marcelo Guillen')
        self.assertEqual(final_data.get('email_detectado'), 'guillen.marce@gmail.com')

    @patch('services.municipio_responder.llamar_gemini')
    @patch('services.municipio_responder._handle_ticket_creation')
    @patch('services.municipio_responder.extract_description_and_check_confirmation')
    def test_simple_confirmation_proceeds_to_creation(self, mock_check_confirmation, mock_handle_creation, mock_llamar_gemini):
        """
        Tests that a simple 'si' in the confirmation state proceeds to ticket creation.
        """
        mock_llamar_gemini.return_value = {"message_body": "mock"} # Prevent None error
        mock_check_confirmation.return_value = (None, True) # Simulate a "yes"
        mock_handle_creation.return_value = ({"message_body": "Ticket creado"}, {})

        initial_claim_data = {
            'categoria': 'Luminaria',
            'ubicacion': 'bousquet isidoro 5500',
            'nombre_usuario_detectado': 'Marcelo Guillen'
        }
        contexto_municipio_actual = {
            'estado_conversacion': ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name,
            'datos_parciales_llm_reclamo': initial_claim_data,
            'historial_llm_reclamo': []
        }

        mock_handle_creation.return_value = ({"message_body": "Ticket creado"}, {})

        user_input = "si confirmo"

        full_context = {
            "viewer_user_obj": self.user,
            "chat_db_context_data": {CONTEXTO_MUNICIPIO: contexto_municipio_actual}
        }

        handle_llm_interaction(
            pregunta_str=user_input,
            context=full_context,
            viewer_user=self.user,
            owner_user=None,
            chat_db_context=None,
            contexto_municipio_actual=contexto_municipio_actual
        )

        # Assert that the ticket creation function was called
        mock_handle_creation.assert_called_once()


if __name__ == '__main__':
    unittest.main()
