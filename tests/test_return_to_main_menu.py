import unittest
from app import create_app, db
from models import User, Rubro, ChatSessionContext
from services.municipio_responder import (
    responder_municipio,
    CONTEXTO_MUNICIPIO,
    ConversationState,
)

class ReturnToMainMenuTestCase(unittest.TestCase):
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
        self.chat_context = ChatSessionContext(chat_session_id="return_main", user_id=self.owner_user.id, context_data={})
        db.session.add(self.chat_context)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_volver_al_inicio_from_reclamos_menu(self):
        response1 = responder_municipio(
            {"action": "mostrar_menu_reclamos"},
            self.owner_user,
            self.owner_user.rubro,
            chat_db_context=self.chat_context,
            anon_id="123",
        )
        self.assertTrue(
            any("Volver al inicio" in opt.get("texto", "") for opt in response1.get("options_list", []))
        )

        response2 = responder_municipio(
            "Volver al inicio",
            self.owner_user,
            self.owner_user.rubro,
            chat_db_context=self.chat_context,
            anon_id="123",
        )
        self.assertIn("¿Cómo te puedo ayudar hoy?", response2.get("message_body", ""))
        db.session.commit()
        db.session.refresh(self.chat_context)
        estado = self.chat_context.context_data.get(CONTEXTO_MUNICIPIO, {}).get("estado_conversacion")
        self.assertEqual(estado, ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name)

if __name__ == "__main__":
    unittest.main()
