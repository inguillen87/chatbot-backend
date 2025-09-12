import unittest
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import responder_municipio
from unittest.mock import patch

class ReclamoMenuRepeatTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app('config.TestingConfig')
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        rubro = Rubro(nombre="municipio", clave="municipio")
        db.session.add(rubro)
        self.owner_user = User(
            name="Test Municipio",
            email="test@municipio.com",
            password_hash="test",
            rubro=rubro,
            tipo_chat="municipio",
            municipio_id=1
        )
        db.session.add(self.owner_user)
        db.session.commit()
        self.chat_context = ChatSessionContext(chat_session_id="menu_repeat", user_id=self.owner_user.id)
        db.session.add(self.chat_context)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('services.llm_orchestrator.llamar_llm_con_fallback')
    def test_repeating_reclamo_command_returns_menu(self, mock_llamar_openai):
        # Mock the LLM to return an action that shows the menu
        mock_llamar_openai.return_value = (
            {
                "accion_backend": "mostrar_menu_reclamos",
                "message_body": "Aquí tienes el menú de reclamos."
            },
            {}
        )
        response1 = responder_municipio(
            "iniciar reclamo",
            self.owner_user,
            self.owner_user.rubro,
            chat_db_context=self.chat_context,
            anon_id="123"
        )
        self.assertIn("Iniciar un Reclamo", " ".join(opt["texto"] for opt in response1.get("options_list", [])))

        response2 = responder_municipio(
            "Hacer un Reclamo",
            self.owner_user,
            self.owner_user.rubro,
            chat_db_context=self.chat_context,
            anon_id="123"
        )
        self.assertIn("Iniciar un Reclamo", " ".join(opt["texto"] for opt in response2.get("options_list", [])))
        self.assertGreater(len(response2.get("options_list", [])), 0)

if __name__ == "__main__":
    unittest.main()
