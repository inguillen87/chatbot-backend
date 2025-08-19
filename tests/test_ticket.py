import sys
import os
import unittest
from unittest.mock import patch
from app import create_app
from extensions import db
from models import User, MunicipioTicket, TicketComentario
import json

class TicketRoutesTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.app.config['TESTING'] = True
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['SESSION_TYPE'] = 'filesystem'
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def test_get_chat_mensajes_anon_success(self):
        """
        Tests that an anonymous user can access the chat messages with a valid anon_id.
        """
        with self.app.app_context():
            ticket = MunicipioTicket(anon_id='test-anon-id', estado='nuevo', pregunta='test pregunta', nro_ticket='12345')
            db.session.add(ticket)
            db.session.commit()
            ticket_id = ticket.id

        response = self.client.get(f'/tickets/chat/{ticket_id}/mensajes', headers={'X-Anon-Id': 'test-anon-id'})
        self.assertEqual(response.status_code, 200)

    def test_get_chat_mensajes_anon_failure(self):
        """
        Tests that an anonymous user cannot access the chat messages with an invalid anon_id.
        """
        with self.app.app_context():
            ticket = MunicipioTicket(anon_id='test-anon-id', estado='nuevo', pregunta='test pregunta', nro_ticket='12345')
            db.session.add(ticket)
            db.session.commit()
            ticket_id = ticket.id

        response = self.client.get(f'/tickets/chat/{ticket_id}/mensajes', headers={'X-Anon-Id': 'invalid-anon-id'})
        self.assertEqual(response.status_code, 403)

    def test_get_tickets_includes_personal_info(self):
        """
        Tests that the /tickets endpoint includes the 'informacion_personal_vecino' object.
        """
        with self.app.app_context():
            # 1. Create users: one admin (municipality) and one citizen (vecino)
            admin_user = User(
                name="Municipalidad de Test",
                email="admin@test.gov",
                rol="admin",
                tipo_chat="municipio",
                municipio_id=1
            )
            admin_user.set_password("admin_password")

            vecino_user = User(
                name="Juan Perez",
                email="juan.perez@test.com",
                rol="usuario",
                direccion="Calle Falsa 123",
                empresa_id=admin_user.id
            )
            vecino_user.set_password("vecino_password")

            db.session.add(admin_user)
            db.session.add(vecino_user)
            db.session.commit()

            # 2. Create a ticket associated with the users
            ticket = MunicipioTicket(
                pregunta="Luz quemada",
                municipio_id=admin_user.municipio_id,
                user_id=vecino_user.id,
                nombre_vecino=vecino_user.name,
                direccion=vecino_user.direccion
            )
            db.session.add(ticket)
            db.session.commit()

            # 3. Generate a token for the admin user
            # We need a way to generate a token. For now, let's mock the decorator's effect.
            # A better approach would be to call a token generation utility if it exists.
            # 3. Generate a token for the admin user
            import jwt
            from datetime import datetime, timedelta
            token = jwt.encode(
                {'user_id': admin_user.id, 'exp': datetime.utcnow() + timedelta(days=1)},
                self.app.config['SECRET_KEY'],
                algorithm="HS256"
            )

            response = self.client.get('/tickets', headers={'Authorization': f'Bearer {token}'})

            # 4. Assertions
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.data)

            self.assertIn('tickets', data)
            self.assertEqual(len(data['tickets']), 1)

            ticket_data = data['tickets'][0]
            self.assertIn('informacion_personal_vecino', ticket_data)

            personal_info = ticket_data['informacion_personal_vecino']
            self.assertEqual(personal_info['nombre'], 'Juan Perez')
            self.assertEqual(personal_info['direccion'], 'Calle Falsa 123')
            self.assertIsNone(personal_info['dni'])


if __name__ == '__main__':
    unittest.main()
