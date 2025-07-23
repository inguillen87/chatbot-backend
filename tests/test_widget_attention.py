import unittest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig
from models import User

class WidgetAttentionEndpointTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        with self.app.app_context():
            db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
        self.app_context.pop()

    def test_default_message(self):
        user = User(name='Test User', email='test@example.com', password_hash='test', rol='admin', municipio_id=1)
        db.session.add(user)
        db.session.commit()
        with patch('routes.widget_attention.get_user_by_token') as mock_get_user:
            mock_get_user.return_value = user
            response = self.client.get('/widget/attention-message/some_token')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['message'], 'Hola!👋, en qué podemos ayudarte?')

    def test_random_from_choices(self):
        user = User(name='Test User', email='test@example.com', password_hash='test', rol='admin', municipio_id=1, atencion_widget='Hola,Como estas?,Que tal?')
        db.session.add(user)
        db.session.commit()
        with patch('routes.widget_attention.get_user_by_token') as mock_get_user:
            mock_get_user.return_value = user
            response = self.client.get('/widget/attention-message/some_token')
            self.assertEqual(response.status_code, 200)
            self.assertIn(response.json['message'], ['Hola', 'Como estas?', 'Que tal?'])
