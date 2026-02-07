import pytest
from app import create_app, db
from config import TestingConfig
from models import User, PymePedido, TenantProfile
from utils.auth_helpers import generar_token
import json

class TestPedidosVisibility:
    def setup_method(self):
        self.app = create_app(TestingConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def teardown_method(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_create_pedido_sets_tenant_id(self):
        # 1. Setup Data
        # Create a Pyme User
        pyme = User(name="Pyme Test", email="pyme@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(pyme)
        db.session.commit() # Get ID

        # Create a Tenant linked to this Pyme
        tenant = TenantProfile(
            slug="pyme-test",
            nombre="Pyme Test Store",
            tipo="pyme",
            pyme_id=pyme.id,
            is_active=True
        )
        db.session.add(tenant)
        db.session.commit()

        # NOTE: We intentionally do NOT set pyme.tenant_id here initially,
        # to test if the fallback logic in routes/pedidos.py works (finding tenant by pyme_id).

        # 2. Generate Token
        token = generar_token(pyme.id, pyme.rol, pyme.tipo_chat, None, pyme.id)
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json'
        }

        # 3. Create Order via API
        payload = {
            "detalles": [{"nombre": "Item 1", "cantidad": 1, "precio_unitario": 100}],
            "monto_total": 100,
            "nombre_cliente": "Cliente Test"
        }

        response = self.client.post('/pedidos', headers=headers, data=json.dumps(payload))

        assert response.status_code == 201, f"Response: {response.json}"
        data = response.json
        nro_pedido = data.get('nro_pedido')

        # 4. Verify PymePedido has tenant_id set
        pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
        assert pedido is not None
        assert pedido.pyme_id == pyme.id
        assert pedido.tenant_id == tenant.id, "tenant_id should be resolved from pyme_id link"

    def test_create_pedido_uses_user_tenant_id(self):
        # 1. Setup Data
        pyme = User(name="Pyme User", email="pyme2@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(pyme)
        db.session.flush()

        tenant = TenantProfile(
            slug="pyme-user-link",
            nombre="Pyme User Link",
            tipo="pyme",
            pyme_id=pyme.id
        )
        db.session.add(tenant)
        db.session.flush()

        # Link user to tenant explicitly
        pyme.tenant_id = tenant.id
        db.session.commit()

        # 2. Generate Token
        token = generar_token(pyme.id, pyme.rol, pyme.tipo_chat, None, pyme.id)
        headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}

        # 3. Create Order
        payload = {
            "detalles": [{"nombre": "Item A", "cantidad": 2}],
            "monto_total": 50,
            "nombre_cliente": "Cliente 2"
        }

        response = self.client.post('/pedidos', headers=headers, data=json.dumps(payload))
        assert response.status_code == 201

        # 4. Verify
        nro_pedido = response.json.get('nro_pedido')
        pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido).first()
        assert pedido.tenant_id == tenant.id
