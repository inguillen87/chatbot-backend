import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import MunicipioTicket, User, TicketComentario
from utils.auth_helpers import generar_token


class TicketTimelineTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class TicketTimelineEndpointTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TicketTimelineTestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        user = User(name='Admin', email='timeline-admin@example.test', rol='admin', tipo_chat='municipio', municipio_id=1)
        user.set_password('pass')
        db.session.add(user)
        db.session.commit()
        self.user = user

        ticket = MunicipioTicket(nro_ticket='123456', municipio_id=user.id, pregunta='p', consulta_pin='654321', user_id=user.id)
        db.session.add(ticket)
        db.session.commit()
        self.ticket_id = ticket.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_timeline_endpoint_returns_events(self):
        comentario = TicketComentario(municipio_ticket_id=self.ticket_id, comentario='primer mensaje')
        db.session.add(comentario)
        db.session.commit()

        token = generar_token(self.user.id, self.user.rol, self.user.tipo_chat, self.user.municipio_id, self.user.pyme_id)
        headers = {'Authorization': f'Bearer {token}'}
        resp = self.client.get(f'/tickets/municipio/{self.ticket_id}/timeline', headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn('timeline', data)
        self.assertGreaterEqual(len(data['timeline']), 2)

if __name__ == '__main__':
    unittest.main()
