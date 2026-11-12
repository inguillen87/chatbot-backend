import unittest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig
from models import User, PymePedido, TenantProfile

class MetricasTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # A prior synthetic account proves fixture ownership is not user ID 1.
        earlier = User(name="Synthetic earlier account", email="earlier-metrics@example.invalid", rol="usuario")
        earlier.set_password("synthetic-earlier-only")
        db.session.add(earlier)
        db.session.commit()

        # Create a test user
        self.user = User(
            name="Test User",
            email="test@example.com",
            rol="admin", tipo_chat="pyme"
        )
        self.user.set_password("password")
        db.session.add(self.user)
        db.session.flush()
        self.user.pyme_id = self.user.id
        db.session.commit()
        self.assertNotEqual(self.user.id, 1)

        self.tenant = TenantProfile(slug="metrics-legacy-fixture", nombre="Synthetic metrics",
                                    tipo="pyme", pyme_id=self.user.id, is_active=True)
        db.session.add(self.tenant)
        db.session.flush()
        self.user.tenant_id = self.tenant.id
        self.user.tenant_slug = self.tenant.slug
        db.session.commit()

        # Create a test order
        self.order = PymePedido(
            pyme_id=self.user.id,
            monto_total=100, tenant_id=self.tenant.id, moneda="ARS",
            asunto="Test Order",
            detalles="{}",
            user_id=self.user.id,
        )
        db.session.add(self.order)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _login(self):
        return self.client.post(
            '/auth/login',
            json={"email": "test@example.com", "password": "password"}
        )

    def test_get_metrics_summary(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/summary',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['total_sales'], 100)
        self.assertEqual(response.json['total_orders'], 1)
        self.assertEqual(response.json['new_customers'], 1)
        self.assertIsNone(response.json['conversion_rate'])
        self.assertEqual(response.json['unique_customers'], 1)
        self.assertEqual(response.json['avg_order_value'], 100)

    def test_get_metrics_kpis(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/kpis',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json['sales_growth'])
        self.assertEqual(response.json['total_sales'], 100)

    def test_get_sales_over_time(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/sales-over-time',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, [])
        self.assertEqual(response.headers["X-Metrics-Availability"], "unavailable")

    def test_get_top_products(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/top-products',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, [])
        self.assertEqual(response.headers["X-Metrics-Availability"], "unavailable")

    def test_get_sales_by_region(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/sales-by-region',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, [])
        self.assertEqual(response.headers["X-Metrics-Availability"], "unavailable")

if __name__ == '__main__':
    unittest.main()
