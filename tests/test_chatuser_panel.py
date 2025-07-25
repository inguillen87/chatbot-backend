import unittest
import uuid
from unittest.mock import patch, MagicMock
from app import create_app, db
from models import ChatSessionContext, User, Rubro
from config import TestConfig


class ChatUserPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(TestConfig)
        cls.app_context = cls.app.app_context()
        cls.app_context.push()
        db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        db.session.remove()
        db.drop_all()
        cls.app_context.pop()

    def setUp(self):
        # Create a dummy rubro and owner user
        self.rubro = Rubro(nombre='Test Rubro', clave='test_rubro')
        db.session.add(self.rubro)
        db.session.commit()

        self.owner_user = User(
            email='owner@test.com',
            name='Test Owner',
            token=str(uuid.uuid4()),
            rubro_id=self.rubro.id,
            nombre_empresa='TestCo',
            rol='admin'
        )
        self.owner_user.set_password('password')
        db.session.add(self.owner_user)
        db.session.commit()

    def tearDown(self):
        db.session.query(ChatSessionContext).delete()
        db.session.query(User).delete()
        db.session.query(Rubro).delete()
        db.session.commit()

    def test_register_and_associate_chat_session(self):
        # 1. Simulate anonymous chat session creation
        anon_id = str(uuid.uuid4())
        chat_session_id = str(uuid.uuid4())

        chat_context = ChatSessionContext(
            chat_session_id=chat_session_id,
            anon_id=anon_id,
            context_data={'some_key': 'some_value'}
        )
        db.session.add(chat_context)
        db.session.commit()

        # 2. Register the user with the same chat session id
        with patch('services.pymes.get_or_create_pyme_user_by_token', return_value=self.owner_user):
            resp = self.client.post('/chatuserregisterpanel', json={
                'name': 'New User',
                'email': 'newuser@example.com',
                'password': 'password123',
                'empresa_token': self.owner_user.token
            }, headers={
                'X-Chat-Session-Id': chat_session_id,
                'X-Anon-Id': anon_id
            })

        self.assertEqual(resp.status_code, 201)
        json_data = resp.get_json()
        self.assertIn('id', json_data)
        new_user_id = json_data['id']

        # 3. Verify the ChatSessionContext was updated
        updated_chat_context = ChatSessionContext.query.get(chat_session_id)
        self.assertIsNotNone(updated_chat_context)
        self.assertEqual(updated_chat_context.user_id, new_user_id)
        self.assertIsNone(updated_chat_context.anon_id)
        self.assertTrue(updated_chat_context.context_data.get('just_logged_in_flag'))

if __name__ == '__main__':
    unittest.main()
