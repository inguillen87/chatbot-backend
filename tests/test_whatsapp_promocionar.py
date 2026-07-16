import unittest
from unittest.mock import patch
from pathlib import Path
import shutil
import jwt
import importlib
from flask import Flask
from extensions import db
from config import TestConfig
from models import User
from tests.auth_test_utils import clerk_superadmin_headers


class WhatsappPromocionarTest(unittest.TestCase):
    def setUp(self):
        promo_module = importlib.import_module('routes.whatsapp_promocionar')
        promo_module = importlib.reload(promo_module)
        self.promo_module = promo_module

        self.app = Flask(__name__)
        self.app.config.from_object(TestConfig)
        db.init_app(self.app)
        self.app.register_blueprint(promo_module.whatsapp_promocionar_bp)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin_user = User(name='Admin', email='a@a.com', password_hash='x', rol='admin')
        self.other_admin = User(name='Other', email='o@o.com', password_hash='x', rol='admin')
        db.session.add_all([self.admin_user, self.other_admin])
        db.session.flush()
        # Admin users are owners in this auth model; employees point to empresa_id.
        self.admin_user.empresa_id = None
        self.other_admin.empresa_id = None

        self.client_user = User(
            name='Cliente1', email='c1@c.com', password_hash='x',
            telefono='+123', acepta_marketing=True, empresa_id=self.admin_user.id
        )
        self.other_client = User(
            name='Cliente2', email='c2@c.com', password_hash='x',
            telefono='+456', acepta_marketing=True, empresa_id=self.other_admin.id
        )
        db.session.add_all([self.client_user, self.other_client])
        db.session.commit()

        self.client = self.app.test_client()
        token = jwt.encode(
            {"user_id": self.admin_user.id},
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        self.auth_headers = {"Authorization": f"Bearer {token}"}

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch('routes.whatsapp_promocionar.enviar_imagen_whatsapp', return_value=True)
    def test_rate_limit(self, mock_send):
        tmp_dir = Path('test_rate_limit_dir')
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        orig = self.promo_module.RATE_LIMIT_DIR
        self.promo_module.RATE_LIMIT_DIR = tmp_dir
        try:
            payload = {
                'titulo': 'Promo',
                'descripcion': 'Desc',
                'link': 'https://x',
                'url_imagen': 'http://img'
            }
            resp1 = self.client.post('/api/whatsapp/promocionar', json=payload, headers=self.auth_headers)
            self.assertEqual(resp1.status_code, 200)
            self.assertEqual(resp1.get_json()['enviados'], 1)
            mock_send.assert_called_once_with(
                self.client_user.telefono,
                'Promo\n\nDesc\nhttps://x',
                'http://img'
            )
            resp2 = self.client.post('/api/whatsapp/promocionar', json=payload, headers=self.auth_headers)
            self.assertEqual(resp2.status_code, 429)
        finally:
            self.promo_module.RATE_LIMIT_DIR = orig
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    @patch('routes.whatsapp_promocionar.enviar_imagen_whatsapp', return_value=True)
    def test_envio_global_registra_para_todos(self, mock_send):
        tmp_dir = Path('test_rate_limit_dir')
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        orig = self.promo_module.RATE_LIMIT_DIR
        self.promo_module.RATE_LIMIT_DIR = tmp_dir
        try:
            # Elevate admin to super_admin to allow global broadcast
            self.admin_user.rol = 'super_admin'
            db.session.commit()
            superadmin_headers = clerk_superadmin_headers(self.admin_user)
            payload = {
                'titulo': 'Promo',
                'descripcion': 'Desc',
                'link': 'https://x',
                'url_imagen': 'http://img',
                'todos': True
            }
            response = self.client.post(
                '/api/whatsapp/promocionar',
                json=payload,
                headers=superadmin_headers,
            )
            self.assertEqual(response.status_code, 200)

            from routes.whatsapp_promocionar import _ultimo_envio, _puede_enviar
            last_global = _ultimo_envio(None)
            self.assertIsNotNone(last_global)
            # Other admin should see the same last send and be blocked
            self.assertEqual(last_global, _ultimo_envio(self.other_admin.id))
            self.assertFalse(_puede_enviar(self.other_admin.id))
        finally:
            self.promo_module.RATE_LIMIT_DIR = orig
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    @patch('routes.whatsapp_promocionar.enviar_imagen_whatsapp', return_value=True)
    def test_estado_promocion(self, mock_send):
        tmp_dir = Path('test_rate_limit_dir')
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        orig = self.promo_module.RATE_LIMIT_DIR
        self.promo_module.RATE_LIMIT_DIR = tmp_dir
        try:
            resp = self.client.get('/api/whatsapp/promocionar', headers=self.auth_headers)
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
            self.client.post('/api/whatsapp/promocionar', json=payload, headers=self.auth_headers)
            resp2 = self.client.get('/api/whatsapp/promocionar', headers=self.auth_headers)
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.get_json()
            self.assertFalse(data2['puede_enviar'])
            self.assertIsNotNone(data2['ultimo_envio'])
        finally:
            self.promo_module.RATE_LIMIT_DIR = orig
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)

    def test_puede_enviar_por_empresa(self):
        tmp_dir = Path('test_rate_limit_dir')
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        orig = self.promo_module.RATE_LIMIT_DIR
        self.promo_module.RATE_LIMIT_DIR = tmp_dir
        try:
            from routes.whatsapp_promocionar import _registrar_envio, _puede_enviar
            _registrar_envio(self.admin_user.id)
            self.assertFalse(_puede_enviar(self.admin_user.id))
            self.assertTrue(_puede_enviar(self.other_admin.id))
        finally:
            self.promo_module.RATE_LIMIT_DIR = orig
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)


if __name__ == '__main__':
    unittest.main()
