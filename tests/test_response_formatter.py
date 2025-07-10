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
        self.assertEqual(response["respuesta"], "Opción web")
        self.assertEqual(len(response["botones"]), 1)
        self.assertEqual(response["botones"][0]["texto"], "Web Opción 1")
        self.assertEqual(response["botones"][0]["action_id"], "web_opt1")
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
        self.assertEqual(len(response["botones"]), 1)
        self.assertEqual(response["botones"][0]["texto"], "Visitar Web")
        self.assertEqual(response["botones"][0]["url"], "https://example.com")
        self.assertEqual(response["botones"][0]["action_id"], "open_url_action") # Default action_id if 'type' is 'url' and no 'action_id' provided


if __name__ == '__main__':
    unittest.main()
