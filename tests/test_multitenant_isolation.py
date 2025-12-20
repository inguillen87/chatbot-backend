import unittest
import uuid
from unittest.mock import patch
from app import app
from models import db, TenantProfile, User

class IsolationTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        self.app.config['TESTING'] = True
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Create Owner User for Tenant A
        email_a = f"usera-{uuid.uuid4()}@example.com"
        self.user_a = User(name='User A', email=email_a, rol='admin', tipo_chat='pyme', es_empleado=True)
        self.user_a.set_password('password')
        db.session.add(self.user_a)
        db.session.flush()

        # Create Tenant A
        slug_a = f"tenant-a-{uuid.uuid4()}"
        self.tenant_a = TenantProfile(slug=slug_a, nombre='Tenant A', tipo='pyme', pyme_id=self.user_a.id)
        db.session.add(self.tenant_a)
        db.session.flush()

        # Link User A to Tenant A
        self.user_a.tenant_id = self.tenant_a.id

        # Create Owner User for Tenant B
        email_b = f"userb-{uuid.uuid4()}@example.com"
        self.user_b = User(name='User B', email=email_b, rol='admin', tipo_chat='pyme', es_empleado=True)
        self.user_b.set_password('password')
        db.session.add(self.user_b)
        db.session.flush()

        # Create Tenant B
        slug_b = f"tenant-b-{uuid.uuid4()}"
        self.tenant_b = TenantProfile(slug=slug_b, nombre='Tenant B', tipo='pyme', pyme_id=self.user_b.id)
        db.session.add(self.tenant_b)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        try:
            db.drop_all()
        except Exception:
            pass # Ignore SQLite FK issues during teardown
        self.app_context.pop()

    @patch('utils.auth_helpers.obtener_token', return_value='mock_token')
    @patch('utils.auth_helpers.user_from_token')
    def test_admin_access_denied_for_other_tenant(self, mock_user_from_token, mock_token):
        # Setup: Authenticated as User A
        mock_user_from_token.return_value = self.user_a

        # Action: Try to access Admin API for Tenant B
        response = self.app.test_client().get(
            f'/api/admin/tenants/{self.tenant_b.slug}/employees',
            headers={'Authorization': 'Bearer mock_token', 'X-Tenant': self.tenant_b.slug}
        )

        # Assert: Should be 403 Forbidden
        self.assertEqual(response.status_code, 403)
        data = response.get_json()
        self.assertTrue('Unauthorized' in data.get('error') or 'denied' in data.get('error'))

    @patch('utils.auth_helpers.obtener_token', return_value='mock_token')
    @patch('utils.auth_helpers.user_from_token')
    def test_admin_access_allowed_for_own_tenant(self, mock_user_from_token, mock_token):
        # Setup: Authenticated as User A
        mock_user_from_token.return_value = self.user_a

        # Action: Access Own Tenant
        response = self.app.test_client().get(
            f'/api/admin/tenants/{self.tenant_a.slug}/employees',
            headers={'Authorization': 'Bearer mock_token', 'X-Tenant': self.tenant_a.slug}
        )

        # Assert: Should be 200 OK (or empty list)
        self.assertEqual(response.status_code, 200)

if __name__ == '__main__':
    unittest.main()
