import os
import unittest

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}


class AuthDemoLoginTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()


    def test_demo_login_uses_isolated_demo_user_not_owner(self):
        resp = self.client.post("/auth/demo", json={"rubro": "municipio"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()

        tenant = TenantProfile.query.filter_by(slug=payload.get("tenant_slug")).first()
        self.assertIsNotNone(tenant)
        owner = User.query.get(getattr(tenant, "municipio_id", None) or getattr(tenant, "pyme_id", None))

        self.assertTrue((payload.get("email") or "").startswith("demo."))
        if owner is not None:
            self.assertNotEqual(payload.get("id"), owner.id)
            self.assertNotEqual(payload.get("email"), owner.email)

    def test_demo_login_returns_token_and_demo_mode(self):
        resp = self.client.post("/auth/demo", json={"rubro": "municipio"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get("demo_mode"))
        self.assertIn("token", payload)
        self.assertIn("tenant_slug", payload)

        decoded = jwt.decode(payload["token"], self.app.config["SECRET_KEY"], algorithms=["HS256"])
        self.assertTrue(decoded.get("demo_mode"))
        self.assertEqual(decoded.get("tenant_slug"), payload.get("tenant_slug"))

    def test_demo_login_rejects_unknown_rubro(self):
        resp = self.client.post("/auth/demo", json={"rubro": "no-existe-xyz"})
        self.assertEqual(resp.status_code, 404)

    def test_demo_login_rejects_existing_non_demo_tenant_slug(self):
        owner = User(
            email="owner-private@test.com",
            name="Owner Private",
            rol="admin",
            tipo_chat="pyme",
        )
        owner.set_password("password")
        db.session.add(owner)
        db.session.commit()

        tenant = TenantProfile(
            slug="private-tenant",
            nombre="Private Tenant",
            tipo="pyme",
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post("/auth/demo", json={"rubro": "private-tenant"})
        self.assertEqual(resp.status_code, 404)


    def test_demo_catalog_returns_superadmin_and_languages(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('super_admin_demo', payload)
        self.assertTrue(payload.get('demo_login_enabled'))
        self.assertEqual(payload.get('demo_login_endpoint'), '/auth/demo')
        self.assertEqual(payload.get('demo_login_methods'), ['POST'])
        self.assertTrue((payload.get('quick_login_payload') or {}).get('tenant_slug'))
        self.assertEqual(payload['super_admin_demo']['role'], 'super_admin')
        languages = payload.get('supported_languages') or []
        codes = {item.get('code') for item in languages}
        self.assertTrue({'es', 'en', 'pt'}.issubset(codes))
        tenant_demos = payload.get('tenant_demos') or []
        self.assertTrue(all(item.get('enabled') is True for item in tenant_demos))
        self.assertTrue(all(item.get('login_endpoint') == '/auth/demo' for item in tenant_demos))

    def test_demo_catalog_can_bootstrap_superadmin(self):
        resp = self.client.get('/auth/demo/catalog?ensure_users=true')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        email = payload['super_admin_demo']['email']
        user = User.query.filter_by(email=email).first()
        self.assertIsNotNone(user)
        self.assertIn(user.rol, {'super_admin', 'superadmin'})



    def test_demo_login_accepts_generic_pyme_entrypoint(self):
        resp = self.client.post("/auth/demo", json={"rubro": "pyme"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("tipo_chat"), "pyme")
        self.assertTrue(payload.get("demo_mode"))

    def test_demo_catalog_exposes_quick_login_payload(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get('demo_login_methods'), ['POST'])
        quick = payload.get('quick_login_payload') or {}
        self.assertIsInstance(quick.get('tenant_slug'), str)
        self.assertTrue(quick.get('tenant_slug'))

    def test_demo_login_works_without_explicit_rubro(self):
        resp = self.client.post('/auth/demo', json={})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get('demo_mode'))
        self.assertTrue(payload.get('tenant_slug'))


    def test_demo_login_includes_admin_dashboard_fields_and_cookie(self):
        resp = self.client.post('/auth/demo', json={"rubro": "municipio"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn('tenant_id', payload)
        self.assertIn('municipio_id', payload)
        self.assertIn('marketplace', payload)
        set_cookie_header = resp.headers.get('Set-Cookie', '')
        self.assertIn('auth_token=', set_cookie_header)

    def test_demo_catalog_exposes_generic_entry_points(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        entry_points = payload.get('entry_points') or []
        keys = {item.get('key') for item in entry_points}
        self.assertIn('municipio', keys)
        self.assertIn('pyme', keys)
        self.assertTrue(all(item.get('enabled') is True for item in entry_points))


    def test_demo_login_works_when_demo_registry_is_empty(self):
        original = self.app.config.get('DEMO_RUBROS')
        self.app.config['DEMO_RUBROS'] = []
        try:
            resp = self.client.post('/auth/demo', json={'rubro': 'municipio'})
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload.get('tipo_chat') in {'municipio', 'pyme'})
            self.assertTrue(payload.get('tenant_slug'))
        finally:
            self.app.config['DEMO_RUBROS'] = original

    def test_demo_catalog_exposes_fallback_entries_when_registry_empty(self):
        original = self.app.config.get('DEMO_RUBROS')
        self.app.config['DEMO_RUBROS'] = []
        try:
            resp = self.client.get('/auth/demo/catalog')
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            tenant_demos = payload.get('tenant_demos') or []
            keys = {item.get('key') for item in tenant_demos}
            self.assertTrue(keys)
            self.assertIn('pyme', keys)
        finally:
            self.app.config['DEMO_RUBROS'] = original


if __name__ == "__main__":
    unittest.main()
