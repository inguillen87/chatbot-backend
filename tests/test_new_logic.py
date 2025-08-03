import unittest
import os
import json
from unittest.mock import patch, MagicMock

# Import the function to be tested
from script_extract_tramites_links import fetch_and_structure_tramites, format_for_final_json
from services.municipios import responder_municipio
from services.chatbot_prompts import JULES_SYSTEM_PROMPT

class TestNewLogic(unittest.TestCase):

    @patch('script_extract_tramites_links.requests.get')
    def test_scraper_generates_non_empty_json(self, mock_requests_get):
        """
        Tests that the scraper script creates a non-empty tramites.json file.
        """
        # Mock the response from requests.get
        mock_response = MagicMock()
        mock_response.status_code = 200
        # A simplified version of the page's HTML structure
        mock_response.text = """
        <html><body><div class="entry-content">
            <div class="et_pb_accordion_item">
                <h5 class="et_pb_toggle_title">MESA DE ENTRADA</h5>
                <div class="et_pb_toggle_content">
                    <p><a href="/?page_id=1603">FUNCIONES</a></p>
                </div>
            </div>
            <div class="et_pb_accordion_item">
                <h5 class="et_pb_toggle_title">RENTAS</h5>
                <div class="et_pb_toggle_content">
                    <p><a href="/?page_id=346">VENCIMIENTOS</a></p>
                </div>
            </div>
        </div></body></html>
        """
        mock_requests_get.return_value = mock_response

        # Run the scraper functions
        structured_data = fetch_and_structure_tramites()
        self.assertIsNotNone(structured_data)
        self.assertIsInstance(structured_data, dict)
        self.assertGreater(len(structured_data), 0, "The scraper should find at least one section.")

        final_data = format_for_final_json(structured_data)
        self.assertIsNotNone(final_data)
        self.assertIsInstance(final_data, dict)
        self.assertGreater(len(final_data), 0, "The final JSON data should not be empty.")

        # Check that the file is created and has content
        output_path = "data/municipios/default/tramites.json"
        if os.path.exists(output_path):
            os.remove(output_path) # Clean up before test

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(final_data, f)

        self.assertTrue(os.path.exists(output_path))
        with open(output_path, "r", encoding="utf-8") as f:
            content = f.read()
            self.assertGreater(len(content), 2, "The tramites.json file should not be empty.")

        # Clean up the created file
        os.remove(output_path)

    @patch('services.municipios.handle_llm_interaction')
    def test_responder_municipio_uses_llm_flow(self, mock_handle_llm_interaction):
        """
        Tests that the responder_municipio function calls the LLM-driven handle_llm_interaction function.
        """
        # Mock the LLM handler response
        mock_llm_response = {
            "message_body": "LLM Handled",
            "options_list": [],
            "message_type": "text",
            "fuente": "llm_test"
        }
        mock_handle_llm_interaction.return_value = (mock_llm_response, {})

        # Mock users and context
        mock_owner_user = MagicMock()
        mock_owner_user.id = 1
        mock_viewer_user = MagicMock()
        mock_viewer_user.id = 2
        mock_chat_db_context = MagicMock()
        mock_chat_db_context.context_data = {}

        # Call the function
        response = responder_municipio(
            pregunta_original="Hola, ¿qué tal?",
            owner_user=mock_owner_user,
            rubro_obj=None,
            viewer_user=mock_viewer_user,
            chat_db_context=mock_chat_db_context
        )

        # Assert that handle_llm_interaction was called
        mock_handle_llm_interaction.assert_called_once()

        # Assert that the response from the LLM handler is returned
        self.assertEqual(response["message_body"], "LLM Handled")
        self.assertEqual(response["fuente"], "llm_test")

        self.assertTrue(JULES_SYSTEM_PROMPT.startswith("# **Tu Misión**"))

if __name__ == '__main__':
    unittest.main()
