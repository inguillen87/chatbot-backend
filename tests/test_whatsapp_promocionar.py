import unittest
from unittest.mock import patch
from pathlib import Path
from flask import Flask
from extensions import db
from config import TestConfig
from models import User


class WhatsappPromocionarTest(unittest.TestCase):
    def setUp(self):
        # Bypass authentication
        self.token_patcher = patch('utils.auth_helpers.token_requerido', lambda f: (lambda *a, **k: f(None, *a, **k)))
        self.admin_patcher = patch('utils.auth_helpers.admin_o_empleado_requerido', lambda f: (lambda *a, **k: f(*a, **k)))
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
        user = User(name='Test', email='t@t.com', password_hash='x', telefono='+123', acepta_marketing=True)
        db.session.add(user)
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
            resp1 = self.client.post('/api/whatsapp/promocionar', json={'mensaje': 'Hola', 'url_imagen': 'http://img'})
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(resp1.get_json()['enviados'], 1)
            resp2 = self.client.post('/api/whatsapp/promocionar', json={'mensaje': 'Hola', 'url_imagen': 'http://img'})
            self.assertEqual(resp2.status_code, 429)
        if tmp.exists():
            tmp.unlink()


if __name__ == '__main__':
    unittest.main()
