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

        tenant = TenantProfile.query.filter_by(slug="new-city-verify").first()
        self.assertIsNotNone(tenant)

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
