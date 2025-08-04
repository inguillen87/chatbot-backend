import unittest
import json
from services.response_formatter import build_interactive_response

class TestResponseFormatter(unittest.TestCase):

    def test_whatsapp_interactive_buttons_1_option(self):
        response = build_interactive_response(
            options=[{"id": "opt1", "texto": "Opción Uno"}],
            body_text="Elige una:",
            channel="whatsapp",
            message_type='interactive_buttons'
            # recipient_id="whatsapp:+123" # Removed, not formatter's concern
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "button")
        self.assertEqual(interactive_content["body"]["text"], "Elige una:")
        self.assertEqual(len(interactive_content["action"]["buttons"]), 1)
        self.assertEqual(interactive_content["action"]["buttons"][0]["reply"]["id"], "opt1")
        self.assertEqual(interactive_content["action"]["buttons"][0]["reply"]["title"], "Opción Uno")

    def test_whatsapp_interactive_buttons_3_options(self):
        options = [
            {"id": "btn1", "texto": "Botón 1"},
            {"id": "btn2", "texto": "Botón 2"},
            {"id": "btn3", "texto": "Botón 3"},
        ]
        response = build_interactive_response(
            options=options, body_text="Selecciona:", channel="whatsapp", message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "button")
        self.assertEqual(len(interactive_content["action"]["buttons"]), 3)
        self.assertEqual(interactive_content["action"]["buttons"][2]["reply"]["title"], "Botón 3")

    def test_whatsapp_interactive_buttons_too_many_options_truncates(self):
        options = [
            {"id": f"btn{i}", "texto": f"Botón {i}"} for i in range(5)
        ]
        response = build_interactive_response(
            options=options, body_text="Demasiados botones:", channel="whatsapp", message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "button")
        self.assertEqual(len(interactive_content["action"]["buttons"]), 3) # Should truncate to 3

    def test_whatsapp_interactive_list_1_option(self):
        response = build_interactive_response(
            options=[{"id": "li1", "texto": "Lista Item 1", "description": "Desc 1"}],
            body_text="Elige de la lista:",
            channel="whatsapp",
            message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "list")
        self.assertEqual(interactive_content["body"]["text"], "Elige de la lista:")
        self.assertEqual(interactive_content["action"]["button"], "Ver opciones") # Default button text
        self.assertEqual(len(interactive_content["action"]["sections"]), 1)
        self.assertEqual(len(interactive_content["action"]["sections"][0]["rows"]), 1)
        self.assertEqual(interactive_content["action"]["sections"][0]["rows"][0]["id"], "li1")
        self.assertEqual(interactive_content["action"]["sections"][0]["rows"][0]["title"], "Lista Item 1")
        self.assertEqual(interactive_content["action"]["sections"][0]["rows"][0]["description"], "Desc 1")


    def test_whatsapp_interactive_list_10_options(self):
        options = [{"id": f"li{i}", "texto": f"Lista Item {i}"} for i in range(10)]
        response = build_interactive_response(
            options=options, body_text="Elige:", channel="whatsapp", message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "list")
        self.assertEqual(len(interactive_content["action"]["sections"][0]["rows"]), 10)

    def test_whatsapp_interactive_list_too_many_options_truncates(self):
        options = [{"id": f"li{i}", "texto": f"Lista Item {i}"} for i in range(15)]
        response = build_interactive_response(
            options=options, body_text="Demasiadas opciones de lista:", channel="whatsapp", message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["type"], "list")
        self.assertEqual(len(interactive_content["action"]["sections"][0]["rows"]), 10) # Should truncate to 10

    def test_whatsapp_text_message(self):
        response = build_interactive_response(
            options=[], body_text="Hola mundo", channel="whatsapp", message_type='text'
        )
        self.assertEqual(response["type"], "text")
        self.assertEqual(response["text"]["body"], "Hola mundo")

    def test_whatsapp_fallback_to_text_if_no_options_for_interactive(self):
        response = build_interactive_response(
            options=[], body_text="Sin opciones", channel="whatsapp", message_type='interactive_buttons'
        )
        # The formatter should now fallback to text if options are empty for an interactive type
        self.assertEqual(response["type"], "text")
        self.assertEqual(response["text"]["body"], "Sin opciones")

    def test_whatsapp_list_section_and_button_text_from_original_response(self):
        original_bot_response_data = {
            "interactive_list_button_text": "Acciones",
            "interactive_list_section_title": "Mis Acciones"
        }
        response = build_interactive_response(
            options=[{"id": "li1", "texto": "Item 1"}],
            body_text="Elige:",
            channel="whatsapp",
            message_type='interactive_list',
            original_bot_response=original_bot_response_data
        )
        self.assertEqual(response["type"], "interactive")
        interactive_content = response["interactive"]
        self.assertEqual(interactive_content["action"]["button"], "Acciones")
        self.assertEqual(interactive_content["action"]["sections"][0]["title"], "Mis Acciones")

    def test_whatsapp_list_row_description_optional(self):
        response = build_interactive_response(
            options=[
                {"id": "li1", "texto": "Item Con Desc", "description": "Esta es una descripción"},
                {"id": "li2", "texto": "Item Sin Desc"}
            ],
            body_text="Elige:",
            channel="whatsapp",
            message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        rows = response["interactive"]["action"]["sections"][0]["rows"]
        self.assertEqual(rows[0]["description"], "Esta es una descripción")
        self.assertNotIn("description", rows[1]) # Description should be absent if empty

    def test_web_response_structure_buttons(self):
        options = [{"id": "web_opt1", "texto": "Web Opción 1"}]
        original_context = {"fuente": "test_web_sugg"}
        response = build_interactive_response(
            options=options, body_text="Opción web", channel="web",
            message_type='interactive_buttons', original_bot_response=original_context
        )
        # Test the HOTFIX behavior
        expected_text = "Opción web\n\n➡️ Web Opción 1"
        self.assertEqual(response["respuesta"], expected_text)
        self.assertEqual(len(response["botones"]), 0) # Buttons are flattened
        self.assertEqual(response["fuente"], "test_web_sugg")

    def test_web_response_structure_text(self):
        original_context = {"fuente": "test_web_plain"}
        response = build_interactive_response(
            options=[], body_text="Texto web", channel="web",
            message_type='text', original_bot_response=original_context
        )
        self.assertEqual(response["respuesta"], "Texto web")
        self.assertEqual(len(response["botones"]), 0) # No buttons for text type
        self.assertEqual(response["fuente"], "test_web_plain")

    def test_web_response_url_button(self):
        options = [{"id": "web_url", "texto": "Visitar Web", "type": "url", "url": "https://example.com"}]
        response = build_interactive_response(
            options=options, body_text="Enlace:", channel="web", message_type='interactive_buttons'
        )
        # Test the HOTFIX behavior
        expected_text = "Enlace:\n\n➡️ Visitar Web"
        self.assertEqual(response["respuesta"], expected_text)
        self.assertEqual(len(response["botones"]), 0) # Buttons are flattened


    def test_web_response_with_audio_url(self):
        """
        Test that the web formatter includes the audio_url when provided.
        """
        bot_response = {
            "message_body": "This is a test.",
            "audio_url": "/static/audio/test.mp3"
        }
        formatted_response = build_interactive_response(
            options=[],
            body_text=bot_response["message_body"],
            channel="web",
            original_bot_response=bot_response
        )
        self.assertIn("audio_url", formatted_response)
        self.assertEqual(formatted_response["audio_url"], "/static/audio/test.mp3")
        self.assertEqual(formatted_response["respuesta"], "This is a test.")

    def test_web_response_without_audio_url(self):
        """
        Test that the web formatter handles responses without an audio_url.
        """
        bot_response = {"message_body": "This is a test."}
        formatted_response = build_interactive_response(
            options=[],
            body_text=bot_response["message_body"],
            channel="web",
            original_bot_response=bot_response
        )
        self.assertIn("audio_url", formatted_response)
        self.assertIsNone(formatted_response["audio_url"])
        self.assertEqual(formatted_response["respuesta"], "This is a test.")

    def test_whatsapp_response_with_audio_url(self):
        """
        Test that the WhatsApp formatter creates an audio message payload.
        """
        audio_url = "https://example.com/audio.mp3"
        formatted_response = build_interactive_response(
            options=[],
            body_text="This is a caption.",
            channel="whatsapp",
            audio_url=audio_url
        )
        expected_payload = {
            "type": "audio",
            "audio": {"link": audio_url}
        }
        self.assertEqual(formatted_response, expected_payload)

    def test_whatsapp_response_without_audio_url(self):
        """
        Test that the WhatsApp formatter falls back to a text message.
        """
        formatted_response = build_interactive_response(
            options=[],
            body_text="This is a standard text message.",
            channel="whatsapp",
            audio_url=None # Explicitly None
        )
        expected_payload = {
            "type": "text",
            "text": {"body": "This is a standard text message."}
        }
        self.assertEqual(formatted_response, expected_payload)


if __name__ == '__main__':
    unittest.main()
