import unittest
import uuid
from app import create_app, db
from models import User, Rubro
from config import TestConfig

class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a dummy rubro and owner user
        rubro = Rubro(nombre='Test Rubro', clave='test_rubro')
        db.session.add(rubro)
        db.session.commit()

        self.owner_user = User(
            email='owner@test.com',
            name='Test Owner',
            entity_token=str(uuid.uuid4()),
            rubro_id=rubro.id,
            nombre_empresa='TestCo',
        )
        self.owner_user.set_password('password')
        db.session.add(self.owner_user)
        db.session.commit()

        self.user = User(
            email='test@test.com',
            name='Test User',
            empresa_id=self.owner_user.id,
        )
        self.user.set_password('password')
        db.session.add(self.user)
        db.session.commit()


    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_chatuser_login_with_entity_token(self):
        """Test that a user can log in using an entity_token."""
        response = self.client.post('/auth/chatuserloginpanel', json={
            'email': 'test@test.com',
            'password': 'password',
            'empresa_token': self.owner_user.entity_token
        })
        self.assertEqual(response.status_code, 200)
        json_data = response.get_json()
        self.assertEqual(json_data['email'], 'test@test.com')

if __name__ == '__main__':
    unittest.main()
