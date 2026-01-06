import unittest
import os

from app import create_app, db
from config import Config
from models import User, ChatSessionContext
from services.municipio_responder import CONTEXTO_MUNICIPIO, ConversationState


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = 'sqlite:///:memory:'


class WelcomeFlowTestCase(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("OPENAI_API_KEY", "test")
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_greeting_initializes_context(self):
        owner = User(name='Muni', email='m@x.com', rol='empresa', tipo_chat='municipio', municipio_id=1)
        owner.set_password('x')
        db.session.add(owner)
        db.session.commit()
        session_ctx = ChatSessionContext(chat_session_id='whatsapp_test', user_id=owner.id, anon_id='anon', context_data={})
        db.session.add(session_ctx)
        db.session.commit()

        from services.logic import responder_chatboc
        response = responder_chatboc(
            'hola', owner_user=owner, rubro_obj=None,
            chat_db_context=session_ctx, tipo_chat='municipio',
            anon_id='anon', chat_session_uuid='uuid', channel='whatsapp'
        )

        self.assertIsInstance(response, dict)
        self.assertIn('message_body', response)
        municipio_ctx = session_ctx.context_data.get(CONTEXTO_MUNICIPIO, {})
        self.assertIn('estado_conversacion', municipio_ctx)


if __name__ == '__main__':
    unittest.main()
