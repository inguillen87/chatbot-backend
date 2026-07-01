import unittest
import json
import jwt
from datetime import datetime, timedelta
from app import create_app, db
from models import User, TenantProfile, Role, UserRole, TenantConfig
from config import TestConfig

class TestAdminTenantVerification(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def generate_token(self, user):
        return jwt.encode(
            {'user_id': user.id, 'exp': datetime.utcnow() + timedelta(days=1)},
            self.app.config['SECRET_KEY'],
            algorithm="HS256"
        )

    def test_create_tenant(self):
        payload = {
            "slug": "new-city-verify",
            "nombre": "New City Verify",
            "tipo": "municipio"
        }
        response = self.client.post('/api/admin/tenants', json=payload)
        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertEqual(data['slug'], "new-city-verify")
        self.assertEqual(data["plan"], "free")
        self.assertFalse(data["integration_access"]["enabled"])
        self.assertEqual(data["integration_access"]["reason_code"], "plan_full_required")

        tenant = TenantProfile.query.filter_by(slug="new-city-verify").first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.plan, "free")
        self.assertNotIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual((tenant.configuracion or {})["provisioning"]["status"], "plan_required")

    def test_create_tenant_honors_full_plan_and_prepares_onboarding(self):
        self.app.config.update(
            TWILIO_TENANT_AUTO_BOOTSTRAP_ENABLED=True,
            TWILIO_TENANT_AUTO_PROVISION_ENABLED=False,
        )
        payload = {
            "slug": "junin-full-verify",
            "nombre": "Junin Full Verify",
            "tipo": "municipio",
            "plan": "full",
            "owner_email": "admin@junin-full-verify.test",
        }

        response = self.client.post('/api/admin/tenants', json=payload)

        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["plan"], "full")
        self.assertEqual(data["tenant"]["plan"], "full")
        self.assertTrue(data["widget_token"])
        self.assertTrue(data["integration_access"]["enabled"])
        self.assertEqual(data["integration_access"]["status"], "enabled")
        self.assertIsNone(data["integration_access"]["reason_code"])
        self.assertEqual(data["whatsapp_onboarding"]["contract_version"], "tenant.whatsapp_onboarding.v1")
        self.assertEqual(data["whatsapp_onboarding"]["provider"], "twilio_tech_provider")

        tenant = TenantProfile.query.filter_by(slug="junin-full-verify").first()
        self.assertIsNotNone(tenant)
        self.assertEqual(tenant.plan, "full")
        self.assertIn("widget_tokens", tenant.configuracion or {})
        self.assertIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual((tenant.configuracion or {})["provisioning"]["status"], "created")
        owner = db.session.get(User, tenant.municipio_id)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.plan, "full")
        self.assertEqual(owner.tenant_slug, tenant.slug)

    def test_create_tenant_rejects_invalid_plan(self):
        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "invalid-plan-verify",
                "nombre": "Invalid Plan Verify",
                "tipo": "pyme",
                "plan": "moonshot",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("plan must be one of", response.get_json()["error"])
        self.assertIsNone(TenantProfile.query.filter_by(slug="invalid-plan-verify").first())

    def test_create_colegio_without_owner_email_creates_synthetic_admin(self):
        response = self.client.post(
            '/api/admin/tenants',
            json={
                "slug": "colegio-meta-review",
                "nombre": "Colegio Meta Review",
                "tipo": "colegio",
            },
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        data = response.get_json()
        self.assertEqual(data["tenant"]["tipo"], "colegio")
        self.assertTrue(data["tenant"]["owner_email_generated"])
        self.assertEqual(data["tenant"]["owner_email"], "admin@colegio-meta-review.chatboc.local")

        tenant = TenantProfile.query.filter_by(slug="colegio-meta-review").first()
        self.assertIsNotNone(tenant)
        self.assertIsNone(tenant.municipio_id)
        self.assertIsNotNone(tenant.pyme_id)

        owner = db.session.get(User, tenant.pyme_id)
        self.assertEqual(owner.rol, "admin_colegio")
        self.assertEqual(owner.tipo_chat, "colegio")
        self.assertEqual(owner.tenant_slug, "colegio-meta-review")
        self.assertEqual(owner.tenant_id, tenant.id)

    def test_create_tenant_auto_prepares_twilio_provider_plan(self):
        self.app.config.update(
            TWILIO_ACCOUNT_SID="ACparent",
            TWILIO_AUTH_TOKEN="parent-secret",
            TWILIO_META_APP_ID="meta-app",
            TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID="cfg-123",
            TWILIO_TECH_PROVIDER_LIVE_ENABLED=False,
            TWILIO_TENANT_AUTO_PROVISION_ENABLED=True,
            PUBLIC_API_BASE_URL="https://api.chatboc.ar",
        )
        response = self.client.post(
            "/api/admin/tenants",
            json={"slug": "ferreteria-demo", "nombre": "Ferreteria Demo", "tipo": "pyme"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertFalse(payload["integration_access"]["enabled"])
        self.assertEqual(payload["integration_access"]["reason_code"], "plan_full_required")
        tenant = TenantProfile.query.filter_by(slug="ferreteria-demo").first()
        self.assertNotIn("twilio_tech_provider", tenant.configuracion or {})
        self.assertNotIn("whatsapp_onboarding", tenant.configuracion or {})
        self.assertEqual(tenant.configuracion["provisioning"]["status"], "plan_required")
        self.assertEqual(tenant.configuracion["provisioning"]["blocked_reason"], "plan_full_required")

    def test_create_employee_with_tenant_context(self):
        # 1. Create Admin User first
        admin = User(email="admin@tenant1.com", name="Admin", rol="admin", tipo_chat="municipio")
        admin.set_password("pass")
        db.session.add(admin)
        db.session.flush()

        # 2. Create Tenant
        tenant = TenantProfile(slug="tenant-1", nombre="Tenant 1", tipo="municipio", municipio_id=admin.id)
        db.session.add(tenant)
        db.session.flush()

        admin.tenant_id = tenant.id
        db.session.commit()

        token = self.generate_token(admin)

        # 3. Call create_employee
        headers = {
            'Authorization': f'Bearer {token}',
            'X-Tenant': 'tenant-1'
        }
        payload = {
            "email": "emp@tenant1.com",
            "name": "Employee 1",
            "password": "pass"
        }

        response = self.client.post('/api/admin/employees', json=payload, headers=headers)
        self.assertEqual(response.status_code, 201)

        emp = User.query.filter_by(email="emp@tenant1.com").first()
        self.assertIsNotNone(emp)
        self.assertEqual(emp.tenant_id, tenant.id)
        self.assertTrue(emp.es_empleado)

        user_role = UserRole.query.filter_by(user_id=emp.id).first()
        self.assertIsNotNone(user_role)
        self.assertEqual(user_role.role.name, "empleado")

    def test_assign_role(self):
        admin = User(email="admin@tenant2.com", name="Admin", rol="admin", tipo_chat="pyme")
        admin.set_password("pass")
        db.session.add(admin)
        db.session.flush()

        tenant = TenantProfile(slug="tenant-2", nombre="Tenant 2", tipo="pyme", pyme_id=admin.id)
        db.session.add(tenant)
        db.session.flush()

        admin.tenant_id = tenant.id

        emp = User(email="emp@tenant2.com", name="Emp", tenant_id=tenant.id)
        emp.set_password("pass")
        db.session.add(emp)
        db.session.commit()

        token = self.generate_token(admin)
        headers = {'Authorization': f'Bearer {token}', 'X-Tenant': 'tenant-2'}

        payload = {"role": "manager"}
        response = self.client.post(f'/api/admin/employees/{emp.id}/roles', json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)

        user_role = UserRole.query.filter_by(user_id=emp.id, tenant_id=tenant.id).all()
        roles = [ur.role.name for ur in user_role]
        self.assertIn("manager", roles)

if __name__ == '__main__':
    unittest.main()
