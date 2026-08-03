import unittest
import json
from unittest.mock import patch, MagicMock
from app import create_app
from extensions import db
from models import PymePedido, TenantProfile, User
from config import TestingConfig

class TestPedidos(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a pyme user for testing
        self.pyme_user = User(
            name="Test Pyme",
            email="pyme@test.com",
            tipo_chat="pyme",
            rol="admin",
            tenant_slug="test-pyme",
        )
        self.pyme_user.set_password("pyme_password")
        db.session.add(self.pyme_user)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="test-pyme",
            nombre="Test Pyme",
            tipo="pyme",
            pyme_id=self.pyme_user.id,
            is_active=True,
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.pyme_user.tenant_id = self.tenant.id
        db.session.commit()

        # Get a token for the pyme user
        res = self.client.post('/auth/login', data=json.dumps({
            "email": "pyme@test.com",
            "password": "pyme_password"
        }), content_type='application/json')
        self.pyme_token = json.loads(res.data)['token']

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_create_pedido(self):
        """Test creating a new order."""
        detalles = [{"producto": "Test Product", "cantidad": 2, "precio": 10.0}]
        res = self.client.post('/pedidos', headers={
            "Authorization": f"Bearer {self.pyme_token}"
        }, data=json.dumps({
            "asunto": "Test Order",
            "detalles": detalles,
            "monto_total": 20.0,
            "nombre_cliente": "Test Client"
        }), content_type='application/json')

        self.assertEqual(res.status_code, 201)
        data = json.loads(res.data)
        self.assertEqual(data['asunto'], "Test Order")
        self.assertEqual(data['detalles'], detalles)
        self.assertEqual(data['monto_total'], 20.0)
        self.assertEqual(data['pyme_id'], self.pyme_user.id)
        self.assertEqual(data['tenant_id'], self.tenant.id)

    def test_list_pedidos(self):
        """Test listing orders for a pyme."""
        # Create a test order
        pedido = PymePedido(
            pyme_id=self.pyme_user.id,
            tenant_id=self.tenant.id,
            asunto="List Test Order",
            detalles=json.dumps([{"producto": "Test Product", "cantidad": 1}]),
            monto_total=10.0
        )
        db.session.add(pedido)
        db.session.commit()

        res = self.client.get('/pedidos', headers={
            "Authorization": f"Bearer {self.pyme_token}"
        })

        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(len(data['pedidos']), 1)
        self.assertEqual(data['pedidos'][0]['asunto'], "List Test Order")

    @patch('routes.pedidos.enviar_email_pedido_admin')
    def test_update_pedido_status_and_send_email(self, mock_enviar_email):
        """Test updating an order's status and verifying email is sent."""
        # Create a test order
        pedido = PymePedido(
            pyme_id=self.pyme_user.id,
            tenant_id=self.tenant.id,
            asunto="Update Test Order",
            detalles=json.dumps([{"producto": "Test Product", "cantidad": 1}]),
            monto_total=10.0
        )
        db.session.add(pedido)
        db.session.commit()

        res = self.client.put(f'/pedidos/{pedido.id}/estado', headers={
            "Authorization": f"Bearer {self.pyme_token}"
        }, data=json.dumps({
            "estado": "confirmado"
        }), content_type='application/json')

        self.assertEqual(res.status_code, 200)
        data = json.loads(res.data)
        self.assertEqual(data['estado'], "confirmado")

        # Verify that the email function was called
        mock_enviar_email.assert_called_once_with(pedido)

if __name__ == '__main__':
    unittest.main()
