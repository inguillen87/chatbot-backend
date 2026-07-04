import unittest

from app import create_app, db
from config import TestConfig
from models import PymePedido, TenantProfile, User


class PublicOrderTrackingPrivacyTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_order_tracking_redacts_private_customer_fields(self):
        owner = User(
            email="owner-public-order@test.com",
            name="Bodega Publica",
            password_hash="hash",
            rol="admin",
            tipo_chat="pyme",
            nombre_empresa="Bodega Publica",
        )
        db.session.add(owner)
        db.session.flush()

        tenant = TenantProfile(
            slug="bodega-publica",
            nombre="Bodega Publica",
            tipo="pyme",
            pyme_id=owner.id,
            logo_url="https://cdn.example.com/logo.png",
        )
        db.session.add(tenant)
        db.session.flush()

        order = PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant.id,
            asunto="Pedido sensible",
            detalles='[{"nombre_producto":"Caja Malbec","cantidad":1,"precio_unitario_original":12000,"subtotal_con_descuento":12000,"moneda":"ARS"}]',
            monto_total=12000,
            nombre_cliente="Marcelo Guillén",
            email_cliente="marcelo@example.com",
            telefono_cliente="+5492611111111",
            direccion="Don Bosco 55, Junin",
            latitud=-33.08,
            longitud=-68.47,
        )
        order.nro_pedido = "PED-PUBLIC-1"
        db.session.add(order)
        db.session.commit()

        response = self.client.get("/api/public/pyme/pedidos/PED-PUBLIC-1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "public.order_tracking.v1")
        self.assertTrue(payload["privacy"]["pii_redacted"])
        self.assertEqual(payload["nro_pedido"], "PED-PUBLIC-1")
        self.assertEqual(payload["pyme_nombre"], "Bodega Publica")
        self.assertEqual(payload["nombre_cliente"], "Ma***")
        self.assertIsNone(payload["email_cliente"])
        self.assertIsNone(payload["telefono_cliente"])
        self.assertEqual(payload["direccion"], "Direccion registrada")
        self.assertIsNone(payload["latitud"])
        self.assertIsNone(payload["longitud"])
        self.assertNotIn("user_id", payload)
        self.assertNotIn("customer_identity", payload)
        self.assertNotIn("Don Bosco", str(payload))
        self.assertNotIn("marcelo@example.com", str(payload))
        self.assertNotIn("+5492611111111", str(payload))


if __name__ == "__main__":
    unittest.main()
