import unittest
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, project_root)

from app import create_app, db
from config import TestConfig
from models import User, Rubro, ChatSessionContext, MunicipioTicket
from services.municipio_responder import responder_municipio, ConversationState

class TestConsultaReclamoFlow(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        rubro = Rubro(id=1, clave='municipio', nombre='municipio')
        owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro, municipio_id=1)
        owner_user.set_password('password')
        db.session.add_all([rubro, owner_user])

        ticket = MunicipioTicket(nro_ticket='123456', municipio_id=1, categoria='Luminaria', estado='en_proceso')
        db.session.add(ticket)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_consulta_estado_reclamo(self):
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_consulta_session', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        payload = {"action": "consultar_estado_reclamo"}
        response = responder_municipio(
            pregunta_original=payload,
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context
        )

        self.assertIn("ingresá el número", response["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_NUMERO_TICKET.name
        )

        response2 = responder_municipio(
            pregunta_original='123456',
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context
        )

        self.assertIn("en_proceso", response2["message_body"])
        self.assertIsNone(chat_context.context_data['contexto_municipio_v2'].get('estado_conversacion'))

    def test_consulta_estado_reclamo_text_action(self):
        owner_user = User.query.get(1)
        rubro_obj = owner_user.rubro
        chat_context = ChatSessionContext(chat_session_id='test_consulta_text_session', user_id=1, context_data={})
        db.session.add(chat_context)
        db.session.commit()

        response = responder_municipio(
            pregunta_original='consultar_estado_reclamo',
            owner_user=owner_user,
            rubro_obj=rubro_obj,
            viewer_user=owner_user,
            chat_db_context=chat_context
        )

        self.assertIn("ingresá el número", response["message_body"])
        self.assertEqual(
            chat_context.context_data['contexto_municipio_v2']['estado_conversacion'],
            ConversationState.ESPERANDO_NUMERO_TICKET.name
        )

if __name__ == '__main__':
    unittest.main()
