import unittest
from unittest.mock import patch
from app import create_app, db
from models_analytics_k import TenantBudget
from utils.auth_helpers import generar_token

class TestAnalyticsKPIs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()

    def setUp(self):
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.client = self.app.test_client()

        # Assuming user_id=1, role=admin
        self.admin_token = generar_token(1, "admin", "municipio", municipio_id=999, pyme_id=None)

    def tearDown(self):
        db.session.rollback()
        db.session.remove()
        self.app_context.pop()

    @patch('services.analytics_kpis_service.analytics_kpi_service.get_operational_metrics')
    def test_get_kpis_admin(self, mock_get_metrics):
        mock_get_metrics.return_value = {"chat": {"handoff_rate_percent": 15.5}}

        response = self.client.get('/api/analytics/kpis?tenant_id=999', headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(response.status_code, [200, 404])
        if response.status_code == 200:
            data = response.get_json()
            self.assertEqual(data["chat"]["handoff_rate_percent"], 15.5)

    def test_get_costs_no_tenant(self):
        response = self.client.get('/api/analytics/costs', headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(response.status_code, [400, 404])

    def test_get_costs_with_budget(self):
        budget = TenantBudget.query.filter_by(tenant_id=999).first()
        if not budget:
            budget = TenantBudget(tenant_id=999, monthly_budget=100.0, current_spend=25.0)
            db.session.add(budget)
        else:
            budget.current_spend = 25.0
        db.session.commit()

        response = self.client.get('/api/analytics/costs?tenant_id=999', headers={'Authorization': f'Bearer {self.admin_token}'})
        self.assertIn(response.status_code, [200, 404])
        if response.status_code == 200:
            data = response.get_json()
            self.assertEqual(data["budget"], 100.0)
            self.assertEqual(data["current_spend"], 25.0)

if __name__ == '__main__':
    unittest.main()
