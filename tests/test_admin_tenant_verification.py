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
        self.assertIn("whatsapp_onboarding", data)
        self.assertEqual(data["whatsapp_onboarding"]["provider"], "twilio_tech_provider")
        self.assertEqual(
            data["whatsapp_onboarding"]["connect"]["tech_provider_endpoint"],
            "/api/v2/tenants/new-city-verify/whatsapp/tech-provider",
        )

        tenant = TenantProfile.query.filter_by(slug="new-city-verify").first()
        self.assertIsNotNone(tenant)
        self.assertIn("whatsapp_onboarding", tenant.configuracion)

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
        onboarding = payload["whatsapp_onboarding"]
        self.assertTrue(onboarding["auto_provision_enabled"])
        self.assertEqual(onboarding["connect"]["embedded_signup_endpoint"], "/api/v2/tenants/ferreteria-demo/whatsapp/tech-provider/embedded-signup")
        tenant = TenantProfile.query.filter_by(slug="ferreteria-demo").first()
        self.assertEqual(tenant.configuracion["twilio_tech_provider"]["status"], "provisioning_plan_ready")
        self.assertEqual(tenant.configuracion["twilio_tech_provider"]["voice_vertical"], "pyme")
        self.assertEqual(tenant.configuracion["twilio_tech_provider"]["voice_intent"], "atencion")

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
