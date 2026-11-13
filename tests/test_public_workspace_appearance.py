"""Full Flask routes + existing disposable SQL fixtures; no browser/provider proof."""
from copy import deepcopy
import json
import tempfile
import unittest

from flask import g
from tests.profile_acceptance_runtime import create_disposable_app


class PublicWorkspaceAppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="brand-public-http-")
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)

    @classmethod
    def tearDownClass(cls):
        from database import db
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from database import db
        from models import TenantProfile
        from services.organization_branding import KEY
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.config["TENANT_ALIASES"] = {}
        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = None
        with self.app.app_context():
            for slug in ("acceptance-a", "acceptance-b"):
                tenant = db.session.get(TenantProfile, self.accounts[slug]["tenant_id"])
                tenant.plan = "full"
                tenant.is_active = True
                tenant.configuracion = {"keep_public_fixture": True}
            db.session.commit()
        self.publish("acceptance-a", "#6D28D9", "#C2410C")
        self.publish("acceptance-b", "#166534", "#334155")

    def publish(self, slug, primary, accent):
        client = self.app.test_client()
        login = client.post("/auth/login", json={"email": self.accounts[slug]["email"], "password": self.password})
        self.assertEqual(login.status_code, 200)
        self.assertTrue(login.json.get("token"))
        current = client.get(f"/api/admin/tenants/{slug}/config")
        self.assertEqual(current.status_code, 200)
        brand = current.json["organization_branding"]
        saved = client.put(brand["save_endpoint"], json={"expected_revision": brand["revision"], "organization_branding": {"operation": "publish", "values": {"enabled": True, "primary_color": primary, "accent_color": accent}}})
        self.assertEqual(saved.status_code, 200)
        self.assertTrue(saved.json["saved"])

    def appearance(self, body, slug, primary):
        self.assertIn("workspace_appearance", body)
        projected = body["workspace_appearance"]
        self.assertIsNotNone(projected)
        self.assertEqual(set(projected), {"contract_version", "tenant", "version", "appearance"})
        self.assertEqual(projected["contract_version"], "organization.workspace_appearance.v1")
        self.assertEqual(projected["tenant"], {"id": self.accounts[slug]["tenant_id"], "slug": slug})
        self.assertEqual(set(projected["appearance"]), {"active", "primary", "accent"})
        self.assertIs(projected["appearance"]["active"], True)
        self.assertEqual(projected["appearance"]["primary"]["background"], primary)
        self.assertNotIn("gradient", projected["appearance"])
        self.assertNotIn("logo_dark_url", projected["appearance"])
        return projected

    def snapshot(self):
        from database import db
        from models import TenantProfile, AuditEvent
        from services.organization_branding import KEY, read_record, revision
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"])
            record = read_record(tenant)
            return {"config": deepcopy(tenant.configuracion), "record": record, "revision": revision(tenant, record), "brand_audits": AuditEvent.query.filter(AuditEvent.event_type.like("organization.branding.%")).count()}

    def test_pwa_aliases_and_tenant_keep_same_root_and_nested_projection(self):
        client = self.app.test_client()
        for path in ("/api/pwa/public/tenant-info", "/api/pwa/tenant-info", "/pwa/tenant-info"):
            with self.subTest(path=path):
                response = client.get(path + "?tenant=acceptance-a")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                projected = self.appearance(response.json, "acceptance-a", "#6D28D9")
                self.assertEqual(response.json["tenant"]["workspace_appearance"], projected)
        direct = client.get("/api/pwa/public/tenant?tenant=acceptance-a")
        self.assertEqual(direct.status_code, 200)
        self.appearance(direct.json, "acceptance-a", "#6D28D9")
        self.assertEqual(direct.headers["Cache-Control"], "no-store")

    def test_legacy_public_profile_uses_same_projection_without_private_store(self):
        response = self.app.test_client().get("/api/public/tenant-profile?tenant=acceptance-a")
        self.assertEqual(response.status_code, 200)
        projected = self.appearance(response.json, "acceptance-a", "#6D28D9")
        self.assertEqual(response.json["tenant"]["workspace_appearance"], projected)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        raw = json.dumps(response.json)
        for private in ("organization_workspace_branding", "branding_store", "save_endpoint", "workflow_ui", "primary_picker"):
            self.assertNotIn(private, raw)

    def test_each_explicit_organization_has_its_own_public_identity_and_palette(self):
        client = self.app.test_client()
        for slug, primary in (("acceptance-a", "#6D28D9"), ("acceptance-b", "#166534")):
            response = client.get("/api/pwa/public/tenant-info?tenant=" + slug)
            self.assertEqual(response.status_code, 200)
            self.appearance(response.json, slug, primary)

    def test_downgrade_and_disabled_palette_return_no_public_brand_without_repair(self):
        from database import db
        from models import TenantProfile
        from services.organization_branding import KEY
        for case in ("free", "disabled", "inactive"):
            with self.subTest(case=case):
                with self.app.app_context():
                    tenant = db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"])
                    tenant.plan = "free" if case == "free" else "full"
                    tenant.is_active = case != "inactive"
                    config = deepcopy(tenant.configuracion)
                    config[KEY]["values"]["enabled"] = case != "disabled"
                    tenant.configuracion = config
                    db.session.commit()
                before = self.snapshot()
                for path in ("/api/pwa/public/tenant-info", "/api/public/tenant-profile"):
                    response = self.app.test_client().get(path + "?tenant=acceptance-a")
                    if case == "inactive":
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn("#6D28D9", response.get_data(as_text=True))
                    else:
                        self.assertEqual(response.status_code, 200)
                        self.assertIsNone(response.json["workspace_appearance"])
                        self.assertIsNone(response.json["tenant"]["workspace_appearance"])
                self.assertEqual(before, self.snapshot())

    def test_generic_legacy_fallback_has_no_brand_and_unknown_explicit_stays_404(self):
        client = self.app.test_client()
        response = client.get("/api/public/tenant-profile?tenant=municipio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["warning"]["fallback"], "default_tenant")
        self.assertIsNone(response.json["workspace_appearance"])
        self.assertIsNone(response.json["tenant"]["workspace_appearance"])
        for path in ("/api/pwa/public/tenant-info", "/api/public/tenant-profile"):
            unknown = client.get(path + "?tenant=missing-public-brand")
            self.assertEqual(unknown.status_code, 404)
            self.assertNotIn("#6D28D9", unknown.get_data(as_text=True))

    def test_pwa_ambient_tenant_without_explicit_selector_does_not_publish_brand(self):
        from database import db
        from models import TenantProfile
        from routes.pwa_public import _tenant_info_response
        with self.app.test_request_context("/api/pwa/public/tenant-info"):
            tenant = db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"])
            g.tenant_profile = tenant
            body = _tenant_info_response().json
            self.assertIsNone(body["workspace_appearance"])
            self.assertIsNone(body["tenant"]["workspace_appearance"])

    def test_pwa_generic_alias_to_another_tenant_has_no_public_brand(self):
        self.app.config["TENANT_ALIASES"] = {"municipio": "acceptance-b"}
        self.app.config["PUBLIC_CATALOG_DEFAULT_TENANT"] = "acceptance-b"
        response = self.app.test_client().get("/api/pwa/public/tenant-info?tenant=municipio")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["tenant"]["slug"], "acceptance-b")
        self.assertIsNone(response.json["workspace_appearance"])
        self.assertIsNone(response.json["tenant"]["workspace_appearance"])

    def test_unknown_legacy_token_or_number_with_ambient_tenant_has_no_brand(self):
        from database import db
        from models import TenantProfile
        from routes.public_resolver import tenant_profile
        queries = ("widget_token=00000000-0000-4000-8000-000000000000", "whatsapp_destination_number=%2B19999999999")
        for query in queries:
            with self.subTest(query=query), self.app.test_request_context("/api/public/tenant-profile?" + query):
                g.tenant_profile = db.session.get(TenantProfile, self.accounts["acceptance-b"]["tenant_id"])
                response = tenant_profile()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["tenant"]["slug"], "acceptance-b")
                self.assertIsNone(response.json["workspace_appearance"])
                self.assertIsNone(response.json["tenant"]["workspace_appearance"])

    def test_public_reads_keep_store_cas_history_audit_and_generic_model_dto_unchanged(self):
        from database import db
        from models import TenantProfile
        before = self.snapshot()
        client = self.app.test_client()
        for path in ("/api/pwa/public/tenant-info", "/api/pwa/public/tenant", "/api/public/tenant-profile"):
            self.assertEqual(client.get(path + "?tenant=acceptance-a").status_code, 200)
        self.assertEqual(before, self.snapshot())
        with self.app.app_context():
            tenant = db.session.get(TenantProfile, self.accounts["acceptance-a"]["tenant_id"])
            self.assertNotIn("workspace_appearance", tenant.to_public_dict())


if __name__ == "__main__":
    unittest.main()
