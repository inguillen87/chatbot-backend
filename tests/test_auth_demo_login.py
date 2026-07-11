import os
import unittest
from types import SimpleNamespace

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from routes import auth as auth_routes


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    ENABLE_DEMO_MODE = True
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
        self.assertEqual(decoded.get("session_kind"), "demo")
        self.assertEqual(decoded.get("rol"), "demo")
        self.assertEqual(decoded.get("tenant_slug"), payload.get("tenant_slug"))

    def test_demo_account_cannot_use_legacy_or_admin_login(self):
        demo = self.client.post("/auth/demo", json={"rubro": "municipio"}).get_json()

        legacy = self.client.post(
            "/auth/login",
            json={"email": demo["email"], "password": "demo"},
        )
        admin = self.client.post(
            "/auth/admin/login",
            json={"email": demo["email"], "password": "demo"},
        )

        self.assertEqual(legacy.status_code, 403)
        self.assertEqual(legacy.get_json().get("reason_code"), "demo_login_required")
        self.assertEqual(admin.status_code, 403)
        self.assertEqual(admin.get_json().get("reason_code"), "demo_scope_denied")

    def test_demo_token_cannot_open_admin_dashboard(self):
        demo = self.client.post("/auth/demo", json={"rubro": "municipio"}).get_json()

        response = self.client.get(
            "/auth/me/dashboard",
            headers={"Authorization": f"Bearer {demo['token']}"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get("reason_code"), "demo_scope_denied")

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


    def test_demo_catalog_returns_languages_without_privileged_credentials(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertNotIn('super_admin_demo', payload)
        self.assertNotIn('password', str(payload).lower())
        self.assertTrue(payload.get('demo_login_enabled'))
        self.assertTrue(payload.get('request_id'))
        self.assertEqual(payload.get('demo_login_endpoint'), '/auth/demo')
        self.assertEqual(payload.get('demo_login_methods'), ['POST'])
        self.assertTrue((payload.get('quick_login_payload') or {}).get('tenant_slug'))
        languages = payload.get('supported_languages') or []
        codes = {item.get('code') for item in languages}
        self.assertTrue({'es', 'en', 'pt'}.issubset(codes))
        self.assertTrue(resp.headers.get('X-Request-Id'))
        tenant_demos = payload.get('tenant_demos') or []
        self.assertTrue(all(item.get('enabled') is True for item in tenant_demos))
        self.assertTrue(all(item.get('login_endpoint') == '/auth/demo' for item in tenant_demos))

    def test_demo_catalog_never_bootstraps_superadmin_from_public_query(self):
        resp = self.client.get('/auth/demo/catalog?ensure_users=true')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertNotIn('super_admin_demo', payload)
        privileged = User.query.filter(User.rol.in_(['super_admin', 'superadmin', 'platform_admin'])).all()
        self.assertEqual(privileged, [])



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

    def test_demo_login_exposes_onboarding_starter_prompts(self):
        resp = self.client.post('/auth/demo', json={"rubro": "municipio"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()

        onboarding = payload.get("demo_onboarding") or {}
        self.assertTrue(onboarding.get("autostart_chat"))
        self.assertTrue(onboarding.get("open_widget"))
        self.assertTrue(onboarding.get("entry_prompt"))
        self.assertTrue(len(onboarding.get("starter_prompts") or []) >= 3)
        self.assertTrue(len(onboarding.get("suggested_workflows") or []) >= 1)
        self.assertIsInstance(onboarding.get("quick_actions"), list)
        self.assertTrue(onboarding.get("experience_version"))
        self.assertIn("satisfaccion_usuario", onboarding.get("analytics_kpis") or [])
        integrations = onboarding.get("integrations") or {}
        self.assertTrue((integrations.get("webhooks") or {}).get("supported"))
        self.assertTrue("hubspot" in (integrations.get("crm_connectors") or []))

        widget = payload.get("widget") or {}
        self.assertTrue(widget.get("autostart"))
        self.assertEqual(widget.get("tenant_slug"), payload.get("tenant_slug"))

    def test_demo_catalog_exposes_generic_entry_points(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        entry_points = payload.get('entry_points') or []
        keys = {item.get('key') for item in entry_points}
        self.assertIn('municipio', keys)
        self.assertIn('pyme', keys)
        self.assertTrue(all(item.get('enabled') is True for item in entry_points))

    def test_demo_catalog_exposes_onboarding_by_sector(self):
        resp = self.client.get('/auth/demo/catalog')
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get('frontend_contract_version'), '2026-02-demo-onboarding-v2')
        onboarding = payload.get('onboarding') or {}
        self.assertEqual(onboarding.get('default_sector'), 'gobierno')
        twilio_trial = onboarding.get('twilio_trial') or {}
        self.assertEqual(twilio_trial.get('display_number'), '+1 (415) 523-8886')
        self.assertEqual(twilio_trial.get('join_phrase'), 'join brief-yesterday')
        self.assertTrue((twilio_trial.get('wa_deeplink') or '').startswith('https://wa.me/14155238886?text='))

        menus_by_tipo = onboarding.get('menus_by_tipo') or {}
        self.assertTrue(any(item.get('id') == 'reclamos' for item in (menus_by_tipo.get('municipio') or [])))
        self.assertTrue(any(item.get('id') == 'crear_pedido' for item in (menus_by_tipo.get('pyme') or [])))
        experience_templates = onboarding.get('experience_templates') or {}
        self.assertEqual((experience_templates.get('municipio') or {}).get('tenant_type'), 'municipio')
        self.assertEqual((experience_templates.get('pyme') or {}).get('tenant_type'), 'pyme')
        municipio_exp = experience_templates.get('municipio') or {}
        whatsapp_playbook = (municipio_exp.get('channel_playbooks') or {}).get('whatsapp') or {}
        self.assertEqual(whatsapp_playbook.get('activation_phrase'), 'join brief-yesterday')
        self.assertIn('location', whatsapp_playbook.get('media_checks') or [])
        component_pack = municipio_exp.get('component_pack') or {}
        self.assertEqual(component_pack.get('layout'), 'stacked_cards')

        sector_options = onboarding.get('sector_options') or []
        sector_keys = {item.get('key') for item in sector_options}
        self.assertIn('gobierno', sector_keys)
        self.assertIn('empresas', sector_keys)

        gobierno = next((item for item in sector_options if item.get('key') == 'gobierno'), {})
        self.assertEqual((gobierno.get('default_login_payload') or {}).get('rubro'), 'municipio')

        empresas = next((item for item in sector_options if item.get('key') == 'empresas'), {})
        rubros = empresas.get('rubros') or []
        self.assertTrue(all((r.get('tipo_chat') or '').lower() == 'pyme' for r in rubros))
        self.assertTrue(all(isinstance(r.get('menu_preview'), list) for r in rubros))

        frontend = payload.get('frontend') or {}
        selector = frontend.get('demo_selector') or {}
        self.assertEqual(selector.get('mode'), 'sector_first')
        self.assertEqual((selector.get('require_rubro_for_sector') or {}).get('empresas'), True)
        preload_names = {item.get('name') for item in (frontend.get('preload_before_login') or [])}
        self.assertTrue({'demo_catalog', 'tenant_info', 'anon_id'}.issubset(preload_names))

    def test_demo_login_supports_sector_gobierno_without_rubro(self):
        resp = self.client.post('/auth/demo', json={'sector': 'gobierno'})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get('tipo_chat'), 'municipio')
        self.assertEqual(payload.get('sector'), 'gobierno')


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

    def test_demo_slug_resolution_uses_cached_demo_registry(self):
        auth_routes._DEMO_RUBROS_CACHE.update({"items": None, "expires_at": 0.0, "fingerprint": ""})
        calls = {"count": 0}

        def _fake_loader(require_owner=False):
            calls["count"] += 1
            return [
                SimpleNamespace(
                    key="municipio",
                    tipo_chat="municipio",
                    rubro_clave="municipio",
                    label="Municipio",
                    aliases=["gobierno"],
                )
            ]

        original_loader = auth_routes.load_demo_rubros
        auth_routes.load_demo_rubros = _fake_loader
        try:
            first = auth_routes._resolve_demo_tenant_slug("gobierno")
            second = auth_routes._resolve_demo_tenant_slug("municipio")
        finally:
            auth_routes.load_demo_rubros = original_loader

        self.assertEqual(first, "municipio")
        self.assertEqual(second, "municipio")
        self.assertEqual(calls["count"], 1)

    def test_demo_catalog_exposes_fallback_entries_when_registry_empty(self):
        original = self.app.config.get('DEMO_RUBROS')
        self.app.config['DEMO_RUBROS'] = []
        try:
            resp = self.client.get('/auth/demo/catalog')
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(resp.headers.get('X-Request-Id'))
            tenant_demos = payload.get('tenant_demos') or []
            keys = {item.get('key') for item in tenant_demos}
            self.assertTrue(keys)
            self.assertIn('pyme', keys)
        finally:
            self.app.config['DEMO_RUBROS'] = original


if __name__ == "__main__":
    unittest.main()
