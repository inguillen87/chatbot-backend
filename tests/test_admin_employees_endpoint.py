import unittest
import json
try:
    from app import create_app
    from config import Config
    from models import TenantProfile, User, Role, UserRole, db
    from utils.auth_helpers import generar_token
except Exception:
    create_app = None

class _TestConfig(Config):
    TESTING = True
    SESSION_TYPE = "filesystem"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    # Disable foreign keys for SQLite to avoid issues with test setup order
    SQLALCHEMY_ENGINE_OPTIONS = {"execution_options": {"sqlite_foreign_keys": False}}

def _create_tenant_with_users():
    owner = User(
        name="Admin",
        email="admin@example.com",
        password_hash="hash",
        rol="admin",
    )
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="demo",
        nombre="Demo Municipio",
        tipo="municipio",
        municipio=owner,
    )
    db.session.add(tenant)
    db.session.flush()

    owner.tenant_id = tenant.id
    # Use existing helper, relying on X-Tenant header for context
    owner.token = generar_token(owner.id, owner.rol, "municipio", None, None)

    # Create an employee
    employee = User(
        name="Employee",
        email="emp@example.com",
        password_hash="hash",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id
    )
    db.session.add(employee)
    db.session.flush()

    role = Role(name="soporte", description="Soporte")
    db.session.add(role)
    db.session.flush()

    user_role = UserRole(user_id=employee.id, role_id=role.id, tenant_id=tenant.id)
    db.session.add(user_role)

    db.session.commit()
    return owner, tenant, employee

@unittest.skipIf(create_app is None, "Flask not available")
class AdminEmployeesTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner, self.tenant, self.employee = _create_tenant_with_users()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_list_employees_by_slug(self):
        # Test GET /api/admin/tenants/<slug>/employees
        headers = {"Authorization": self.owner.token, "X-Tenant": self.tenant.slug}
        resp = self.client.get(
            f"/api/admin/tenants/{self.tenant.slug}/employees",
            headers=headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(isinstance(data, list))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["email"], "emp@example.com")
        self.assertIn("soporte", data[0]["roles"])

    def test_list_current_tenant_employees(self):
        # Test GET /api/admin/employees (relying on context)
        headers = {"Authorization": self.owner.token, "X-Tenant": self.tenant.slug}
        resp = self.client.get(
            "/api/admin/employees",
            headers=headers
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["email"], "emp@example.com")

if __name__ == "__main__":
    unittest.main()
