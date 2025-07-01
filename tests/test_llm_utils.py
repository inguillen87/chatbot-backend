import unittest
from unittest.mock import patch, MagicMock
import json
import logging

# Configure logging for tests to see output if needed
# logging.basicConfig(level=logging.DEBUG) # Or INFO

from services.llm_utils import (
    extract_multiple_contact_details_llm,
    extract_complaint_details_llm,
    update_conversation_summary_llm
)

class TestLlmUtils(unittest.TestCase):

    def setUp(self):
        # Suppress logging during most tests unless explicitly needed for debugging a test
        # self.logger = logging.getLogger('services.llm_utils')
        # self.previous_level = self.logger.getEffectiveLevel()
        # self.logger.setLevel(logging.ERROR) # Suppress info and warning logs from the module
        pass

    def tearDown(self):
        # self.logger.setLevel(self.previous_level)
        pass

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_full_extraction(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "nombre_cliente": "Jane Doe",
            "telefono_cliente": "555-1234",
            "direccion_cliente": "123 Oak Street, Anytown",
            "email_cliente": "jane.doe@example.com"
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "My name is Jane Doe, call me at 555-1234, I live at 123 Oak Street, Anytown. My email is jane.doe@example.com"
        fields = ['nombre_cliente', 'telefono_cliente', 'direccion_cliente', 'email_cliente']
        expected = {
            "nombre_cliente": "Jane Doe",
            "telefono_cliente": "555-1234",
            "direccion_cliente": "123 Oak Street, Anytown",
            "email_cliente": "jane.doe@example.com"
        }
        result = extract_multiple_contact_details_llm(text, fields)
        self.assertEqual(result, expected)
        self.assertTrue(mock_robust_chat.called)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_partial_extraction(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "nombre_cliente": "John Smith",
            "direccion_cliente": "456 Pine Rd"
            # telefono_cliente is missing in LLM response
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "It's John Smith, and the address is 456 Pine Rd."
        fields_to_extract = ['nombre_cliente', 'telefono_cliente', 'direccion_cliente']
        expected = {
            "nombre_cliente": "John Smith",
            "direccion_cliente": "456 Pine Rd"
        } # telefono_cliente should be absent in result
        result = extract_multiple_contact_details_llm(text, fields_to_extract)
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_llm_returns_null_for_field(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "nombre_cliente": "Peter Pan",
            "telefono_cliente": None, # Explicit null from LLM
            "direccion_cliente": "Neverland"
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "Peter Pan from Neverland, no phone."
        fields_to_extract = ['nombre_cliente', 'telefono_cliente', 'direccion_cliente']
        expected = {
            "nombre_cliente": "Peter Pan",
            "direccion_cliente": "Neverland"
        } # telefono_cliente should be filtered out because its value is None
        result = extract_multiple_contact_details_llm(text, fields_to_extract)
        self.assertEqual(result, expected)


    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_empty_response_from_llm(self, mock_robust_chat):
        mock_robust_chat.return_value = "" # LLM returns empty string
        text = "Some random text."
        fields = ['nombre_cliente', 'telefono_cliente']
        expected = {}
        result = extract_multiple_contact_details_llm(text, fields)
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_json_decode_error(self, mock_robust_chat):
        mock_robust_chat.return_value = "this is not json"
        text = "My info is..."
        fields = ['nombre_cliente']
        expected = {}
        result = extract_multiple_contact_details_llm(text, fields)
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_with_markdown(self, mock_robust_chat):
        mock_response_json_str = json.dumps({"nombre_cliente": "Alice"})
        mock_robust_chat.return_value = f"```json\n{mock_response_json_str}\n```"

        text = "Name is Alice"
        fields = ['nombre_cliente']
        expected = {"nombre_cliente": "Alice"}
        result = extract_multiple_contact_details_llm(text, fields)
        self.assertEqual(result, expected)

    # --- Tests for extract_complaint_details_llm ---
    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_full_extraction(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "categoria_reclamo": "bache",
            "direccion_reclamo": "Elm Street 123",
            "descripcion_reclamo": "A big pothole."
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "There is a huge pothole on Elm Street 123, it's dangerous."
        fields = ['categoria_reclamo', 'direccion_reclamo', 'descripcion_reclamo']
        categories = ['bache', 'luminaria', 'basura']
        expected = {
            "categoria_reclamo": "bache",
            "direccion_reclamo": "Elm Street 123",
            "descripcion_reclamo": "A big pothole."
        }
        result = extract_complaint_details_llm(text, fields, categories)
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_invalid_category_from_llm(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "categoria_reclamo": "unregistered_category",
            "direccion_reclamo": "Oak Avenue 45"
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "The light at Oak Avenue 45 is broken."
        fields = ['categoria_reclamo', 'direccion_reclamo']
        categories = ['bache', 'luminaria', 'basura']
        expected = {"direccion_reclamo": "Oak Avenue 45"}
        result = extract_complaint_details_llm(text, fields, categories)
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_partial_and_valid_category(self, mock_robust_chat):
        mock_response_json_str = json.dumps({
            "categoria_reclamo": "luminaria",
            "descripcion_reclamo": "Streetlight is out."
            # direccion_reclamo missing from LLM response
        })
        mock_robust_chat.return_value = mock_response_json_str

        text = "Streetlight is out, it's very dark."
        fields = ['categoria_reclamo', 'direccion_reclamo', 'descripcion_reclamo']
        categories = ['bache', 'luminaria', 'basura']
        expected = {
            "categoria_reclamo": "luminaria",
            "descripcion_reclamo": "Streetlight is out."
        }
        result = extract_complaint_details_llm(text, fields, categories)
        self.assertEqual(result, expected)

    # --- Tests for update_conversation_summary_llm ---
    @patch('services.llm_utils.robust_chat')
    def test_update_conversation_summary_llm_first_turn(self, mock_robust_chat):
        mock_robust_chat.return_value = "User asked about business hours."
        summary = update_conversation_summary_llm(None, "What are your hours?", "We are open 9-5.", "My PYME")
        self.assertEqual(summary, "User asked about business hours.")
        self.assertTrue(mock_robust_chat.called)

    @patch('services.llm_utils.robust_chat')
    def test_update_conversation_summary_llm_subsequent_turn(self, mock_robust_chat):
        mock_robust_chat.return_value = "User asked about hours, then confirmed they are happy."
        prev_summary = "User inquired about store hours."
        summary = update_conversation_summary_llm(prev_summary, "Great, thanks!", "You're welcome!", "My PYME")
        self.assertEqual(summary, "User asked about hours, then confirmed they are happy.")

    @patch('services.llm_utils.robust_chat')
    def test_update_conversation_summary_llm_empty_or_short_llm_response(self, mock_robust_chat):
        mock_robust_chat.return_value = "Ok." # Too short to be a good summary
        prev_summary = "User was asking about product X."
        summary = update_conversation_summary_llm(prev_summary, "Ok", "Sure.", "My PYME")
        self.assertIsNone(summary) # Should return None to signal reuse of previous

        mock_robust_chat.return_value = "" # Empty response
        summary_empty = update_conversation_summary_llm(prev_summary, "Hi", "Hello", "My PYME")
        self.assertIsNone(summary_empty)


    @patch('services.llm_utils.robust_chat')
    def test_update_conversation_summary_llm_robust_chat_exception(self, mock_robust_chat):
        mock_robust_chat.side_effect = Exception("LLM API error")
        prev_summary = "Initial context."
        summary = update_conversation_summary_llm(prev_summary, "User question", "Bot answer", "My PYME")
        self.assertIsNone(summary) # Should return None, signalling to reuse prev_summary

if __name__ == '__main__':
    unittest.main()

The file `tests/test_llm_utils.py` has now been created with the specified content.

Now I will attempt to run these tests again using the discovery mechanism, specifically targeting this file.
