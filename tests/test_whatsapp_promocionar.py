import unittest
from unittest.mock import patch
from pathlib import Path
from flask import Flask
from extensions import db
from config import TestConfig
from models import User
from functools import wraps


class WhatsappPromocionarTest(unittest.TestCase):
    def setUp(self):
        def fake_token(f):
            @wraps(f)
            def wrapper(*a, **k):
                return f(self.admin_user, *a, **k)
            return wrapper

        def fake_admin(f):
            @wraps(f)
            def wrapper(*a, **k):
                return f(*a, **k)
            return wrapper

        self.token_patcher = patch('utils.auth_helpers.token_requerido', fake_token)
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', fake_admin)
        self.token_patcher.start()
        self.admin_patcher.start()

        promo_module = __import__('routes.whatsapp_promocionar', fromlist=['whatsapp_promocionar_bp'])
        self.promo_module = promo_module

        self.app = Flask(__name__)
        self.app.config.from_object(TestConfig)
        db.init_app(self.app)
        self.app.register_blueprint(promo_module.whatsapp_promocionar_bp)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin_user = User(name='Admin', email='a@a.com', password_hash='x', rol='admin')
        other_admin = User(name='Other', email='o@o.com', password_hash='x', rol='admin')
        db.session.add_all([self.admin_user, other_admin])
        db.session.flush()
        self.client_user = User(
            name='Cliente1', email='c1@c.com', password_hash='x',
            telefono='+123', acepta_marketing=True, empresa_id=self.admin_user.id
        )
        self.other_client = User(
            name='Cliente2', email='c2@c.com', password_hash='x',
            telefono='+456', acepta_marketing=True, empresa_id=other_admin.id
        )
        db.session.add_all([self.client_user, self.other_client])
        db.session.commit()

        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()
        self.token_patcher.stop()
        self.admin_patcher.stop()

    @patch('routes.whatsapp_promocionar.enviar_imagen_whatsapp', return_value=True)
    def test_rate_limit(self, mock_send):
        tmp = Path('test_last_whatsapp.txt')
        if tmp.exists():
            tmp.unlink()
        with patch.object(self.promo_module, 'RATE_LIMIT_FILE', tmp):
            payload = {
                'titulo': 'Promo',
                'descripcion': 'Desc',
                'link': 'https://x',
                'url_imagen': 'http://img'
            }
            resp1 = self.client.post('/api/whatsapp/promocionar', json=payload)
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(resp1.get_json()['enviados'], 1)
            mock_send.assert_called_once_with(
                self.client_user.telefono,
                'Promo\n\nDesc\nhttps://x',
                'http://img'
            )
            resp2 = self.client.post('/api/whatsapp/promocionar', json=payload)
            self.assertEqual(resp2.status_code, 429)
        if tmp.exists():
            tmp.unlink()

    @patch('routes.whatsapp_promocionar.enviar_imagen_whatsapp', return_value=True)
    def test_estado_promocion(self, mock_send):
        tmp = Path('test_last_whatsapp.txt')
        if tmp.exists():
            tmp.unlink()
        with patch.object(self.promo_module, 'RATE_LIMIT_FILE', tmp):
            resp = self.client.get('/api/whatsapp/promocionar')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertTrue(data['puede_enviar'])
            self.assertIsNone(data['ultimo_envio'])

            payload = {
                'titulo': 'Promo',
                'descripcion': 'Desc',
                'link': 'https://x',
                'url_imagen': 'http://img'
            }
            self.client.post('/api/whatsapp/promocionar', json=payload)
            resp2 = self.client.get('/api/whatsapp/promocionar')
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.get_json()
            self.assertFalse(data2['puede_enviar'])
            self.assertIsNotNone(data2['ultimo_envio'])
        if tmp.exists():
            tmp.unlink()


if __name__ == '__main__':
    unittest.main()
