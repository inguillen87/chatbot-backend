import unittest
from unittest.mock import patch

from app import create_app, db
from config import TestConfig
from models import User


class ChatOwnerContextPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Municipalidad de Junín",
            email="junin-owner@example.com",
            rol="admin",
            tipo_chat="municipio",
            token="junin-static-token",
        )
        self.owner.set_password("pass")
        db.session.add(self.owner)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_widget_keeps_municipal_owner_after_name_prompt(self):
        session_id = "junin-widget-session"
        headers = {
            "Origin": "https://chatboc.ar",
            "X-Chat-Session-Id": session_id,
        }

        with patch("services.logic.responder_chatboc") as mock_responder:
            mock_responder.side_effect = [
                {
                    "message_body": "¡Hola! Soy JUNI, el asistente virtual de Municipalidad de Junín. ¿Podrías decirme tu nombre?",
                    "message_type": "text",
                    "fuente": "pedir_nombre_inicial",
                },
                {
                    "message_body": "Perfecto Marcelo, seguimos con Junín.",
                    "message_type": "text",
                    "fuente": "municipio_continuidad",
                },
            ]

            first = self.client.post(
                "/ask/municipio",
                json={"pregunta": "__INIT__", "token": self.owner.token},
                headers=headers,
            )
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.get_json().get("fuente"), "pedir_nombre_inicial")

            second = self.client.post(
                "/ask/municipio",
                json={"pregunta": "Marcelo"},
                headers=headers,
            )

        self.assertEqual(second.status_code, 200)
        payload = second.get_json()
        self.assertNotEqual(payload.get("fuente"), "demo_selector")
        self.assertEqual(payload.get("fuente"), "municipio_continuidad")
        ux_context = payload.get("ux_context") or {}
        self.assertTrue(ux_context.get("trusted_owner"))
        self.assertEqual(ux_context.get("owner_tipo_chat"), "municipio")
        self.assertFalse(ux_context.get("should_render_demo_shell"))
        self.assertEqual(mock_responder.call_count, 2)


if __name__ == "__main__":
    unittest.main()
