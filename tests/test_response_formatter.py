import unittest
import json
from services.response_formatter import build_interactive_response

class TestResponseFormatter(unittest.TestCase):

    def test_whatsapp_interactive_buttons_1_option(self):
        options = [{"id": "opt1", "texto": "Option 1"}]
        body = "Choose one:"
        header = "Header Text"
        footer = "Footer Text"
        response = build_interactive_response(options, body, "whatsapp", 'interactive_buttons', header_text=header, footer_text=footer)

        self.assertEqual(response["messaging_product"], "whatsapp")
        self.assertEqual(response["type"], "interactive")
        interactive = response["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(interactive["header"]["type"], "text")
        self.assertEqual(interactive["header"]["text"], header)
        self.assertEqual(interactive["body"]["text"], body)
        self.assertEqual(interactive["footer"]["text"], footer)
        self.assertEqual(len(interactive["action"]["buttons"]), 1)
        self.assertEqual(interactive["action"]["buttons"][0]["reply"]["id"], "opt1")
        self.assertEqual(interactive["action"]["buttons"][0]["reply"]["title"], "Option 1")

    def test_whatsapp_interactive_buttons_3_options(self):
        options = [
            {"id": "opt1", "texto": "Option 1"},
            {"id": "opt2", "texto": "Option 2"},
            {"id": "opt3", "texto": "Option 3"},
        ]
        body = "Choose one:"
        response = build_interactive_response(options, body, "whatsapp", 'interactive_buttons')

        self.assertEqual(response["type"], "interactive")
        interactive = response["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(len(interactive["action"]["buttons"]), 3)
        self.assertEqual(interactive["action"]["buttons"][2]["reply"]["id"], "opt3")
        self.assertEqual(interactive["action"]["buttons"][2]["reply"]["title"], "Option 3")

    def test_whatsapp_interactive_buttons_too_many_options(self):
        # Logs a warning, but still creates with max 3
        options = [
            {"id": "opt1", "texto": "Option 1"},
            {"id": "opt2", "texto": "Option 2"},
            {"id": "opt3", "texto": "Option 3"},
            {"id": "opt4", "texto": "Option 4"},
        ]
        body = "Choose one:"
        with self.assertLogs(level='WARNING') as log:
            response = build_interactive_response(options, body, "whatsapp", 'interactive_buttons')
            self.assertIn("requires 1-3 options, got 4", log.output[0])

        self.assertEqual(response["type"], "interactive")
        interactive = response["interactive"]
        self.assertEqual(interactive["type"], "button")
        self.assertEqual(len(interactive["action"]["buttons"]), 3) # Takes the first 3

    def test_whatsapp_interactive_list_1_option(self):
        options = [{"id": "item1", "texto": "Item 1", "description": "Desc 1"}]
        body = "Select an item:"
        header = "List Header"
        footer = "List Footer"
        response = build_interactive_response(options, body, "whatsapp", 'interactive_list', header_text=header, footer_text=footer)

        self.assertEqual(response["type"], "interactive")
        interactive = response["interactive"]
        self.assertEqual(interactive["type"], "list")
        self.assertEqual(interactive["header"]["type"], "text")
        self.assertEqual(interactive["header"]["text"], header)
        self.assertEqual(interactive["body"]["text"], body)
        self.assertEqual(interactive["footer"]["text"], footer)
        self.assertEqual(interactive["action"]["button"], "Ver opciones")
        self.assertEqual(len(interactive["action"]["sections"]), 1)
        self.assertEqual(len(interactive["action"]["sections"][0]["rows"]), 1)
        row1 = interactive["action"]["sections"][0]["rows"][0]
        self.assertEqual(row1["id"], "item1")
        self.assertEqual(row1["title"], "Item 1")
        self.assertEqual(row1["description"], "Desc 1")

    def test_whatsapp_interactive_list_10_options(self):
        options = [{"id": f"item{i}", "texto": f"Item {i}"} for i in range(1, 11)]
        body = "Select an item:"
        response = build_interactive_response(options, body, "whatsapp", 'interactive_list')

        self.assertEqual(response["type"], "interactive")
        interactive = response["interactive"]
        self.assertEqual(interactive["type"], "list")
        self.assertEqual(len(interactive["action"]["sections"][0]["rows"]), 10)
        self.assertEqual(interactive["action"]["sections"][0]["rows"][9]["id"], "item10")

    def test_whatsapp_interactive_list_too_many_options(self):
        # Logs a warning, but still creates with max 10
        options = [{"id": f"item{i}", "texto": f"Item {i}"} for i in range(1, 12)]
        body = "Select an item:"
        with self.assertLogs(level='WARNING') as log:
            response = build_interactive_response(options, body, "whatsapp", 'interactive_list')
            self.assertIn("requires 1-10 options, got 11", log.output[0])

        interactive = response["interactive"]
        self.assertEqual(len(interactive["action"]["sections"][0]["rows"]), 10)


    def test_whatsapp_text_message(self):
        body = "This is a plain text message."
        response = build_interactive_response([], body, "whatsapp", 'text')

        self.assertEqual(response["messaging_product"], "whatsapp")
        self.assertEqual(response["type"], "text")
        self.assertEqual(response["text"]["body"], body)
        self.assertNotIn("interactive", response)

    def test_whatsapp_fallback_to_text_if_no_options_for_interactive(self):
        body = "Body for interactive, but no options."
        response = build_interactive_response([], body, "whatsapp", 'interactive_buttons')
        self.assertEqual(response["type"], "text") # Falls back to text
        self.assertEqual(response["text"]["body"], body)

    def test_web_response_with_options(self):
        options = [{"id": "web_opt1", "texto": "Web Option 1", "action": "action_web_1"}]
        body = "Choose for web:"
        original_resp = {"fuente": "test_source", "contexto_actualizado": {"data": "sample"}}
        response = build_interactive_response(options, body, "web", 'interactive_buttons', original_bot_response=original_resp)

        self.assertEqual(response["respuesta"], body)
        self.assertEqual(len(response["botones"]), 1)
        self.assertEqual(response["botones"][0]["texto"], "Web Option 1")
        self.assertEqual(response["botones"][0]["action"], "action_web_1")
        self.assertEqual(response["fuente"], "test_source") # Preserves other keys
        self.assertEqual(response["contexto_actualizado"]["data"], "sample")

    def test_web_response_text_only(self):
        body = "Plain text for web."
        original_resp = {"fuente": "test_source_text"}
        response = build_interactive_response([], body, "web", 'text', original_bot_response=original_resp)

        self.assertEqual(response["respuesta"], body)
        self.assertEqual(response["botones"], []) # Empty list for botones
        self.assertEqual(response["fuente"], "test_source_text")

    def test_web_response_options_use_id_as_action_if_action_missing(self):
        options = [{"id": "web_opt_id_only", "texto": "Web Option ID Only"}]
        body = "Choose for web:"
        response = build_interactive_response(options, body, "web", 'interactive_buttons')
        self.assertEqual(response["botones"][0]["action"], "web_opt_id_only")

    def test_unknown_channel(self):
        options = [{"id": "opt1", "texto": "Option 1"}]
        body = "Test body"
        with self.assertLogs(level='ERROR') as log:
            response = build_interactive_response(options, body, "unknown_channel", 'interactive_buttons')
            self.assertIn("Canal desconocido: unknown_channel", log.output[0])
        self.assertEqual(response.get("error"), "Canal no soportado: unknown_channel")

    def test_whatsapp_list_section_title_logic(self):
        options = [{"id": "item1", "texto": "Item 1"}]
        body = "Select an item:"

        # Case 1: No header, should add default section title
        response_no_header = build_interactive_response(options, body, "whatsapp", 'interactive_list')
        self.assertEqual(response_no_header["interactive"]["action"]["sections"][0]["title"], "Opciones disponibles")

        # Case 2: With header, should not add default section title (it remains None or not present)
        response_with_header = build_interactive_response(options, body, "whatsapp", 'interactive_list', header_text="My List Header")
        self.assertNotIn("title", response_with_header["interactive"]["action"]["sections"][0])


if __name__ == '__main__':
    unittest.main()
