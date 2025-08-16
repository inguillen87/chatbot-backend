import unittest
from unittest.mock import patch
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import create_app, db
from models import User, MunicipioTicket
from config import TestConfig
import jwt
from datetime import datetime, timedelta

class TicketPermissionTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create users
        self.user1 = User(id=1, name='User One', email='user1@test.com', tipo_chat='municipio', municipio_id=1)
        self.user1.set_password('password')
        self.user2 = User(id=2, name='User Two', email='user2@test.com', tipo_chat='municipio', municipio_id=2)
        self.user2.set_password('password')
        self.user3 = User(id=3, name='User Three', email='user3@test.com', tipo_chat='municipio', municipio_id=1)
        self.user3.set_password('password')
        self.user4 = User(id=4, name='User Four', email='user4@test.com', tipo_chat='pyme')
        self.user4.set_password('password')

        # Create tickets
        self.ticket1 = MunicipioTicket(id=1, asunto='Ticket 1', user_id=1, municipio_id=1)
        self.ticket2 = MunicipioTicket(id=2, asunto='Ticket 2', user_id=2, municipio_id=2)

        db.session.add_all([self.user1, self.user2, self.user3, self.user4, self.ticket1, self.ticket2])
        db.session.commit()

        # Generate JWTs for each user
        self.token1 = jwt.encode({'user_id': self.user1.id, 'exp': datetime.utcnow() + timedelta(days=1)}, self.app.config['SECRET_KEY'], algorithm="HS256")
        self.token2 = jwt.encode({'user_id': self.user2.id, 'exp': datetime.utcnow() + timedelta(days=1)}, self.app.config['SECRET_KEY'], algorithm="HS256")
        self.token3 = jwt.encode({'user_id': self.user3.id, 'exp': datetime.utcnow() + timedelta(days=1)}, self.app.config['SECRET_KEY'], algorithm="HS256")
        self.token4 = jwt.encode({'user_id': self.user4.id, 'exp': datetime.utcnow() + timedelta(days=1)}, self.app.config['SECRET_KEY'], algorithm="HS256")


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_user_can_access_own_ticket(self):
        response = self.client.get('/tickets/municipio/1', headers={'Authorization': f'Bearer {self.token1}'})
        self.assertEqual(response.status_code, 200)

    def test_user_cannot_access_ticket_from_other_municipio(self):
        response = self.client.get('/tickets/municipio/2', headers={'Authorization': f'Bearer {self.token1}'})
        self.assertEqual(response.status_code, 403)

    def test_admin_can_access_ticket_from_same_municipio(self):
        self.user3.rol = 'admin'
        db.session.commit()
        response = self.client.get('/tickets/municipio/1', headers={'Authorization': f'Bearer {self.token3}'})
        self.assertEqual(response.status_code, 200)

    def test_pyme_user_cannot_access_municipio_ticket(self):
        response = self.client.get('/tickets/municipio/1', headers={'Authorization': f'Bearer {self.token4}'})
        self.assertEqual(response.status_code, 403)

if __name__ == '__main__':
    unittest.main()
