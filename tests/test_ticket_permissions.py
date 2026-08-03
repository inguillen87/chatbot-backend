import unittest
from unittest.mock import patch
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app import create_app, db
from models import User, MunicipioTicket, TenantProfile
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

        # Create explicit tenant owners. Legacy municipio_id values alone are
        # intentionally insufficient for privileged ticket access.
        owner_a = User(name='Owner A', email='owner-a@test.com', password_hash='test', rol='admin', tipo_chat='municipio')
        owner_b = User(name='Owner B', email='owner-b@test.com', password_hash='test', rol='admin', tipo_chat='municipio')
        owner_pyme = User(name='Owner Pyme', email='owner-pyme@test.com', password_hash='test', rol='admin', tipo_chat='pyme')
        db.session.add_all([owner_a, owner_b, owner_pyme])
        db.session.flush()
        tenant_a = TenantProfile(slug='ticket-permissions-a', nombre='Municipio A', tipo='municipio', municipio_id=owner_a.id)
        tenant_b = TenantProfile(slug='ticket-permissions-b', nombre='Municipio B', tipo='municipio', municipio_id=owner_b.id)
        tenant_pyme = TenantProfile(slug='ticket-permissions-pyme', nombre='Pyme', tipo='pyme', pyme_id=owner_pyme.id)
        db.session.add_all([tenant_a, tenant_b, tenant_pyme])
        db.session.flush()
        owner_a.tenant_id = tenant_a.id
        owner_b.tenant_id = tenant_b.id
        owner_pyme.tenant_id = tenant_pyme.id

        # Create users
        self.user1 = User(name='User One', email='user1@test.com', rol='usuario', tipo_chat='municipio', municipio_id=owner_a.id, tenant_id=tenant_a.id, tenant_slug=tenant_a.slug)
        self.user1.set_password('password')
        self.user2 = User(name='User Two', email='user2@test.com', rol='usuario', tipo_chat='municipio', municipio_id=owner_b.id, tenant_id=tenant_b.id, tenant_slug=tenant_b.slug)
        self.user2.set_password('password')
        self.user3 = User(name='User Three', email='user3@test.com', rol='usuario', tipo_chat='municipio', municipio_id=owner_a.id, tenant_id=tenant_a.id, tenant_slug=tenant_a.slug)
        self.user3.set_password('password')
        self.user4 = User(name='User Four', email='user4@test.com', rol='usuario', tipo_chat='pyme', pyme_id=owner_pyme.id, tenant_id=tenant_pyme.id, tenant_slug=tenant_pyme.slug)
        self.user4.set_password('password')

        # Create tickets
        db.session.add_all([self.user1, self.user2, self.user3, self.user4])
        db.session.flush()
        self.ticket1 = MunicipioTicket(asunto='Ticket 1', user_id=self.user1.id, municipio_id=owner_a.id, tenant_id=tenant_a.id)
        self.ticket2 = MunicipioTicket(asunto='Ticket 2', user_id=self.user2.id, municipio_id=owner_b.id, tenant_id=tenant_b.id)

        db.session.add_all([self.ticket1, self.ticket2])
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
        # Citizen access is exposed through /tickets/mios; the detail endpoint
        # is a tenant backoffice surface and must not be opened to end users.
        detail = self.client.get(f'/tickets/municipio/{self.ticket1.id}', headers={'Authorization': f'Bearer {self.token1}'})
        self.assertEqual(detail.status_code, 404)
        response = self.client.get('/tickets/mios', headers={'Authorization': f'Bearer {self.token1}'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item['id'] for item in response.get_json()['tickets']], [self.ticket1.id])

    def test_user_cannot_access_ticket_from_other_municipio(self):
        response = self.client.get(f'/tickets/municipio/{self.ticket2.id}', headers={'Authorization': f'Bearer {self.token1}'})
        self.assertEqual(response.status_code, 404)

    def test_admin_can_access_ticket_from_same_municipio(self):
        self.user3.rol = 'admin'
        db.session.commit()
        response = self.client.get(f'/tickets/municipio/{self.ticket1.id}', headers={'Authorization': f'Bearer {self.token3}'})
        self.assertEqual(response.status_code, 200)

    def test_pyme_user_cannot_access_municipio_ticket(self):
        response = self.client.get(f'/tickets/municipio/{self.ticket1.id}', headers={'Authorization': f'Bearer {self.token4}'})
        self.assertEqual(response.status_code, 404)

if __name__ == '__main__':
    unittest.main()
