import os

# Enable interactive responses for these tests even though the production
# default now falls back to plain text menus. Set the env var before importing
# the module so the flag is read correctly.
os.environ["WHATSAPP_FORCE_TEXT"] = "false"

import unittest
from unittest import mock
import json
import services.response_formatter as rf
from services.response_formatter import build_interactive_response, render_audio_text

class TestResponseFormatter(unittest.TestCase):

    def test_whatsapp_interactive_buttons_1_option(self):
        response = build_interactive_response(
            options=[{"id": "opt1", "texto": "Opción Uno"}],
            body_text="Elige una:",
            channel="whatsapp",
            message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["type"], "button")
        self.assertEqual(len(response["interactive"]["action"]["buttons"]), 1)
        self.assertEqual(response["interactive"]["action"]["buttons"][0]["reply"]["id"], "opt1")

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
        self.assertEqual(response["interactive"]["type"], "button")
        self.assertEqual(len(response["interactive"]["action"]["buttons"]), 3)
        self.assertEqual(response["interactive"]["action"]["buttons"][2]["reply"]["title"], "Botón 3")

    def test_whatsapp_interactive_buttons_with_image(self):
        response = build_interactive_response(
            options=[{"id": "opt1", "texto": "Opción"}],
            body_text="Elegí:",
            channel="whatsapp",
            message_type='interactive_buttons',
            original_bot_response={"image_url": "http://example.com/pic.jpg"}
        )
        self.assertEqual(response["interactive"]["header"]["type"], "image")
        self.assertEqual(response["interactive"]["header"]["image"]["link"], "http://example.com/pic.jpg")

    def test_whatsapp_interactive_buttons_too_many_options_fallbacks_to_list(self):
        options = [
            {"id": f"btn{i}", "texto": f"Botón {i}"} for i in range(5)
        ]
        response = build_interactive_response(
            options=options, body_text="Demasiados botones:", channel="whatsapp", message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["type"], "list")
        self.assertEqual(len(response["interactive"]["action"]["sections"][0]["rows"]), 5)

    def test_whatsapp_interactive_list_4_options_works(self):
        options = [{"id": f"li{i}", "texto": f"Lista Item {i}"} for i in range(4)]
        response = build_interactive_response(
            options=options,
            body_text="Elige de la lista:",
            channel="whatsapp",
            message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["type"], "list")
        self.assertEqual(len(response["interactive"]["action"]["sections"][0]["rows"]), 4)

    def test_whatsapp_interactive_list_with_sections_override(self):
        options = [
            {"id": "qa1", "texto": "Opción 1"},
            {"id": "qa2", "texto": "Opción 2"},
            {"id": "qa3", "texto": "Opción 3"},
            {"id": "qa4", "texto": "Opción 4"},
        ]
        override = {
            "interactive_list_sections": [
                {
                    "title": "Atajos",
                    "rows": [
                        {"id": "qa1", "title": "Opción 1", "description": "Descripción 1"},
                        {"id": "qa2", "title": "Opción 2", "description": "Descripción 2"},
                        {"id": "qa3", "title": "Opción 3", "description": "Descripción 3"},
                        {"id": "qa4", "title": "Opción 4", "description": "Descripción 4"},
                    ],
                }
            ],
            "interactive_list_button_text": "Abrir demo",
        }

        response = build_interactive_response(
            options=options,
            body_text="Elegí:",
            channel="whatsapp",
            message_type='interactive_list',
            original_bot_response=override,
        )

        action = response["interactive"]["action"]
        self.assertEqual(action["button"], "Abrir demo")
        self.assertEqual(len(action["sections"]), 1)
        self.assertEqual(action["sections"][0]["title"], "Atajos")
        self.assertEqual(len(action["sections"][0]["rows"]), 4)
        self.assertEqual(action["sections"][0]["rows"][0]["description"], "Descripción 1")

    def test_whatsapp_interactive_list_10_options(self):
        options = [{"id": f"li{i}", "texto": f"Lista Item {i}"} for i in range(10)]
        response = build_interactive_response(
            options=options, body_text="Elige:", channel="whatsapp", message_type='interactive_list'
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["type"], "list")
        self.assertEqual(len(response["interactive"]["action"]["sections"][0]["rows"]), 10)

    def test_whatsapp_interactive_list_too_many_options_fallbacks_to_text(self):
        options = [{"id": f"li{i}", "texto": f"Lista Item {i}"} for i in range(15)]
        response = build_interactive_response(
            options=options, body_text="Demasiadas opciones de lista:", channel="whatsapp", message_type='interactive_list'
        )
        self.assertEqual(response["type"], "text")
        expected_body = (
            "Demasiadas opciones de lista:\n\n" +
            "\n".join([f"*{i+1}*. Lista Item {i}" for i in range(15)]) +
            "\n*16*. Menú\n*17*. Cancelar"
        )
        self.assertEqual(response["text"]["body"], expected_body)

    def test_render_audio_text_numbers_options(self):
        text = render_audio_text("Menú", options=[{"texto": "Uno"}, {"texto": "Dos"}])
        self.assertIn("Opciones disponibles:", text)
        self.assertIn("Opción 1: Uno", text)
        self.assertIn("Opción 2: Dos", text)
        self.assertIn("Respondé con el número de la opción que prefieras.", text)

    def test_render_audio_text_includes_summary_for_reclamo(self):
        datos = {
            "categoria": "luminaria",
            "descripcion": "poste caído",
            "ubicacion": "Av. Siempre Viva 742",
            "nombre_usuario_detectado": "Ana García",
            "telefono_detectado": "123456789",
        }
        text = render_audio_text(
            "Gracias, ya registramos tu reclamo.",
            datos=datos,
            accion="crear_reclamo",
        )
        self.assertIn("Resumen del reclamo:", text)
        self.assertIn("Categoría: luminaria", text)
        self.assertIn("Descripción: poste caído", text)
        self.assertIn("Ubicación: Av. Siempre Viva 742", text)
        self.assertIn("Nombre de contacto: Ana García", text)
        self.assertIn("Teléfono: 123456789", text)

    def test_whatsapp_text_message(self):
        response = build_interactive_response(
            options=[], body_text="Hola mundo", channel="whatsapp", message_type='text'
        )
        self.assertEqual(response["type"], "text")
        expected_body = "Hola mundo\n\n*1*. Menú\n*2*. Cancelar"
        self.assertEqual(response["text"]["body"], expected_body)

    def test_whatsapp_text_message_with_image(self):
        response = build_interactive_response(
            options=[],
            body_text="Hola imagen",
            channel="whatsapp",
            message_type='text',
            original_bot_response={"image_url": "http://example.com/pic.jpg"}
        )
        self.assertEqual(response["image_url"], "http://example.com/pic.jpg")

    def test_whatsapp_fallback_to_text_if_no_options_for_interactive(self):
        response = build_interactive_response(
            options=[], body_text="Sin opciones", channel="whatsapp", message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "text")
        expected_body = "Sin opciones\n\n*1*. Menú\n*2*. Cancelar"
        self.assertEqual(response["text"]["body"], expected_body)

    def test_whatsapp_list_section_and_button_text_from_original_response(self):
        original_bot_response_data = {
            "interactive_list_button_text": "Acciones",
            "interactive_list_section_title": "Mis Acciones"
        }
        response = build_interactive_response(
            options=[{"id": f"li{i}", "texto": f"Item {i}"} for i in range(4)],
            body_text="Elige:",
            channel="whatsapp",
            message_type='interactive_list',
            original_bot_response=original_bot_response_data
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["action"]["button"], "Acciones")
        self.assertEqual(response["interactive"]["action"]["sections"][0]["title"], "Mis Acciones")


    def test_whatsapp_list_row_description_optional(self):
        response = build_interactive_response(
            options=[
                {"id": "li1", "texto": "Item Con Desc", "description": "Esta es una descripción"},
                {"id": "li2", "texto": "Item Sin Desc"}
            ],
            body_text="Elige:",
            channel="whatsapp",
            message_type='interactive_buttons' # Should be buttons for 2 options
        )
        self.assertEqual(response["type"], "interactive")
        self.assertEqual(response["interactive"]["type"], "button")

    @unittest.mock.patch.dict(os.environ, {"WHATSAPP_FORCE_TEXT": "true"})
    def test_force_text_via_env_var(self):
        """If WHATSAPP_FORCE_TEXT=true even interactive calls return text."""
        import importlib
        importlib.reload(rf)

        response = build_interactive_response(
            options=[{"id": "a", "texto": "Opción A"}],
            body_text="Menu:",
            channel="whatsapp",
            message_type='interactive_buttons'
        )
        self.assertEqual(response["type"], "text")

        # Restore default for remaining tests
        os.environ["WHATSAPP_FORCE_TEXT"] = "false"
        importlib.reload(rf)

    @unittest.mock.patch.dict(os.environ, {"WHATSAPP_FORCE_TEXT": "true"})
    def test_text_menu_includes_url_and_actionable_numbers(self):
        """URL-only options should be rendered inline while action buttons keep numbering."""
        import importlib
        importlib.reload(rf)

        response = build_interactive_response(
            options=[{"texto": "Ir a ePagos", "url": "https://example.com"}],
            body_text="Pagá",
            channel="whatsapp",
            message_type='interactive_buttons'
        )

        expected_body = (
            "Pagá\n\nIr a ePagos: https://example.com\n\n*1*. Menú\n*2*. Cancelar"
        )

        self.assertEqual(response["type"], "text")
        self.assertEqual(response["text"]["body"], expected_body)

        last_options = response.get("contexto_actualizado", {}).get("last_options_sent", [])
        self.assertEqual(len(last_options), 2)
        self.assertEqual(last_options[0]["action_id"], "menu_principal")

        # Restore module with default env var
        os.environ["WHATSAPP_FORCE_TEXT"] = "false"
        importlib.reload(rf)


    def test_web_response_structure_buttons(self):
        options = [{"id": "web_opt1", "texto": "Web Opción 1"}]
        original_context = {"fuente": "test_web_sugg"}
        response = build_interactive_response(
            options=options, body_text="Opción web", channel="web",
            message_type='interactive_buttons', original_bot_response=original_context
        )
        self.assertEqual(response["respuesta"], "Opción web")
        self.assertEqual(len(response["botones"]), 1)
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
        self.assertEqual(response["respuesta"], "Enlace:")
        self.assertEqual(len(response["botones"]), 1)
        self.assertEqual(response["botones"][0]["url"], "https://example.com")


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
        expected_body = "This is a caption.\n\n*1*. Menú\n*2*. Cancelar"
        expected_payload = {
            "type": "text",
            "text": {"body": expected_body},
            "audio": {"link": audio_url},
            "contexto_actualizado": {"last_options_sent": [
                {"texto": "Menú", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]},
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
        expected_body = "This is a standard text message.\n\n*1*. Menú\n*2*. Cancelar"
        expected_payload = {
            "type": "text",
            "text": {"body": expected_body},
            "contexto_actualizado": {"last_options_sent": [
                {"texto": "Menú", "action_id": "menu_principal"},
                {"texto": "Cancelar", "action_id": "cancelar"},
            ]},
        }
        self.assertEqual(formatted_response, expected_payload)


if __name__ == '__main__':
    unittest.main()
