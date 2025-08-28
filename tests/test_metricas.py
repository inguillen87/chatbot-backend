import unittest
from unittest.mock import patch
from app import create_app, db
from config import TestConfig
from models import User, PymePedido

class MetricasTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        # Create a test user
        self.user = User(
            name="Test User",
            email="test@example.com",
            pyme_id=1
        )
        self.user.set_password("password")
        db.session.add(self.user)
        db.session.commit()

        # Create a test order
        self.order = PymePedido(
            pyme_id=1,
            monto_total=100,
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
        self.assertEqual(response.json['conversion_rate'], 1.0)

    def test_get_metrics_kpis(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/kpis',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['sales_growth'], 0.1)

    def test_get_sales_over_time(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/sales-over-time',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json), 7)

    def test_get_top_products(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/top-products',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json), 5)

    def test_get_sales_by_region(self):
        response = self._login()
        self.assertEqual(response.status_code, 200)
        token = response.json['token']

        response = self.client.get(
            '/api/metrics/sales-by-region',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json), 4)

if __name__ == '__main__':
    unittest.main()
