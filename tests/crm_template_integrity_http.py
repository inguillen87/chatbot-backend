"""Actual CRM routes, normal local login and SQL on disposable fixture data.

Run in a dedicated process: python -m tests.crm_template_integrity_http.
The shared acceptance runtime disables .env loading and all external networks.
This is a SQLite HTTP regression, not customer/provider/production acceptance.
"""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == "__main__":
    prepare_process()

from copy import deepcopy
import os
import tempfile
import unittest
from unittest.mock import patch

from tests.profile_acceptance_runtime import create_disposable_app


class CrmTemplateIntegrityHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["CORS_ALLOWED_ORIGINS"] = "https://panel.example.invalid"
        os.environ["CLERK_SUPERADMIN_EMAILS"] = "guillen.marce@gmail.com"
        cls.directory = tempfile.TemporaryDirectory(prefix="chatboc-crm-templates-")
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        from models import TenantProfile, User, db

        with cls.app.app_context():
            tenant = db.session.get(TenantProfile, cls.accounts["acceptance-a"]["tenant_id"])
            for label, role in (("legacy-manager", "manager"), ("superadmin", "super_admin")):
                user = User(
                    name=label,
                    email=("guillen.marce@gmail.com" if role == "super_admin" else label + "@example.invalid"),
                    rol=role,
                    tenant_id=(None if role == "super_admin" else tenant.id),
                    tenant_slug=(None if role == "super_admin" else tenant.slug),
                    tipo_chat="municipio",
                )
                user.set_password(cls.password)
                db.session.add(user)
                db.session.flush()
                cls.accounts[label] = {"id": user.id, "email": user.email, "tenant_id": user.tenant_id}
            db.session.commit()

    @classmethod
    def tearDownClass(cls):
        from models import db

        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from models import TenantProfile, db

        self.context = self.app.app_context()
        self.context.push()
        self.tenant_a_id = self.accounts["acceptance-a"]["tenant_id"]
        self.tenant_b_id = self.accounts["acceptance-b"]["tenant_id"]
        self.initial = {
            "campaign_templates": [{"slug": "welcome", "name": "Original", "message": "Original message", "version": 3}],
            "institutional_assistant": {"menu": [{"label": "Keep menu"}]},
            "organization_profile_activity": "Keep activity",
            "provider_configuration_marker": {"unchanged": True},
        }
        self.other = {"campaign_templates": [{"slug": "other", "name": "Other", "message": "Other message", "version": 1}], "other": {"keep": True}}
        db.session.get(TenantProfile, self.tenant_a_id).configuracion = deepcopy(self.initial)
        db.session.get(TenantProfile, self.tenant_b_id).configuracion = deepcopy(self.other)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        from models import db

        db.session.remove()
        self.context.pop()

    @staticmethod
    def url(slug="acceptance-a", template_slug=None):
        base = f"/api/admin/tenants/{slug}/campaigns/templates"
        return base + (f"/{template_slug}" if template_slug else "")

    def login(self, account="acceptance-a"):
        response = self.client.post("/auth/login", json={
            "email": self.accounts[account]["email"], "password": self.password,
        })
        self.assertEqual(response.status_code, 200, response.get_json())
        return {"Authorization": "Bearer " + response.get_json()["token"], "X-Tenant": "acceptance-a"}

    def persisted(self, tenant_id=None):
        from models import TenantProfile, db

        db.session.expire_all()
        return deepcopy(db.session.get(TenantProfile, tenant_id or self.tenant_a_id).configuracion)

    def descriptor(self, headers):
        result = self.client.get("/api/admin/tenants/acceptance-a/crm/module-permissions", headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        return result.get_json()

    def test_employee_can_read_but_both_writes_are_denied_before_template_access(self):
        headers = self.login("viewer")
        self.assertFalse(self.descriptor(headers)["can_manage"])
        result = self.client.get(self.url(), headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertEqual(result.get_json()["templates"][0]["name"], "Original")

        with patch("routes.crm.routes._tenant_templates", side_effect=AssertionError("Denied actor must not read template storage")), patch(
            "routes.crm.routes._save_tenant_templates", side_effect=AssertionError("Denied actor must not mutate template storage")
        ):
            for method, endpoint, body in (
                ("POST", self.url(), {"slug": "new", "name": "New", "message": "New message"}),
                ("PUT", self.url(template_slug="welcome"), {"name": "Denied", "message": "Denied"}),
            ):
                with self.subTest(method=method):
                    result = self.client.open(endpoint, method=method, json=body, headers=headers)
                    self.assertEqual(result.status_code, 403, result.get_json())
                    self.assertEqual(result.get_json()["reason_code"], "tenant_admin_required")
                    self.assertEqual(result.get_json()["action_hint"], "ask_admin")
        self.assertEqual(self.persisted(), self.initial)
        self.assertEqual(self.persisted(self.tenant_b_id), self.other)

    def test_own_admin_create_persists_after_expiration_and_preserves_other_configuration(self):
        headers = self.login()
        self.assertTrue(self.descriptor(headers)["can_manage"])
        response = self.client.post(self.url(), json={"slug": "new", "name": "New template", "message": "New message"}, headers=headers)
        self.assertEqual(response.status_code, 201, response.get_json())
        stored = self.persisted()
        self.assertEqual([entry["slug"] for entry in stored["campaign_templates"]], ["welcome", "new"])
        self.assertEqual(stored["campaign_templates"][0], self.initial["campaign_templates"][0])
        self.assertEqual(stored["campaign_templates"][1]["message"], "New message")
        for key in set(self.initial) - {"campaign_templates"}:
            self.assertEqual(stored[key], self.initial[key])
        self.assertEqual(self.persisted(self.tenant_b_id), self.other)
        reread = self.client.get(self.url(), headers=headers)
        self.assertEqual(reread.get_json()["templates"], stored["campaign_templates"])

    def test_own_admin_update_persists_after_expiration_and_keeps_existing_fields(self):
        headers = self.login()
        response = self.client.put(self.url(template_slug="welcome"), json={"name": "Updated"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        stored = self.persisted()
        self.assertEqual(stored["campaign_templates"][0]["name"], "Updated")
        self.assertEqual(stored["campaign_templates"][0]["message"], "Original message")
        self.assertEqual(stored["campaign_templates"][0]["version"], 4)
        for key in set(self.initial) - {"campaign_templates"}:
            self.assertEqual(stored[key], self.initial[key])
        self.assertEqual(self.persisted(self.tenant_b_id), self.other)

    def test_foreign_tenant_writes_do_not_access_templates(self):
        headers = self.login()
        headers["X-Tenant"] = "acceptance-b"
        with patch("routes.crm.routes._tenant_templates", side_effect=AssertionError("Foreign tenant must not load templates")):
            for method, endpoint, body in (
                ("POST", self.url("acceptance-b"), {"slug": "new", "name": "New", "message": "New"}),
                ("PUT", self.url("acceptance-b", "other"), {"name": "Denied"}),
            ):
                with self.subTest(method=method):
                    response = self.client.open(endpoint, method=method, json=body, headers=headers)
                    self.assertEqual(response.status_code, 403, response.get_json())
                    self.assertEqual(response.get_json()["reason_code"], "tenant_access_denied")
        self.assertEqual(self.persisted(), self.initial)
        self.assertEqual(self.persisted(self.tenant_b_id), self.other)

    def test_exact_scoped_tenant_admin_assignment_matches_descriptor_and_write_guard(self):
        headers = self.login("delegated")
        self.assertTrue(self.descriptor(headers)["can_manage"])
        response = self.client.put(self.url(template_slug="welcome"), json={"name": "Delegated admin"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.persisted()["campaign_templates"][0]["name"], "Delegated admin")

    def test_admin_assignment_for_other_tenant_cannot_authorize_template_changes(self):
        from models import Role, UserRole, db

        role = Role.query.filter_by(name="tenant_admin").one()
        grant = UserRole(user_id=self.accounts["viewer"]["id"], role_id=role.id, tenant_id=self.tenant_b_id)
        db.session.add(grant)
        db.session.commit()
        try:
            headers = self.login("viewer")
            self.assertFalse(self.descriptor(headers)["can_manage"])
            response = self.client.put(self.url(template_slug="welcome"), json={"name": "Denied grant"}, headers=headers)
            self.assertEqual(response.status_code, 403, response.get_json())
            self.assertEqual(self.persisted(), self.initial)
        finally:
            db.session.delete(grant)
            db.session.commit()

    def test_legacy_manager_without_explicit_admin_authority_is_read_only(self):
        from models import User, db
        from utils.auth_helpers import generar_token

        actor = db.session.get(User, self.accounts["legacy-manager"]["id"])
        token = generar_token(actor.id, actor.rol, actor.tipo_chat, actor.municipio_id, actor.pyme_id)
        headers = {"Authorization": "Bearer " + token, "X-Tenant": "acceptance-a"}
        self.assertFalse(self.descriptor(headers)["can_manage"])
        response = self.client.put(self.url(template_slug="welcome"), json={"name": "Denied manager"}, headers=headers)
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(self.persisted(), self.initial)

    def test_allowlisted_superadmin_preserves_existing_template_edit_contract(self):
        from models import User, db
        from tests.auth_test_utils import clerk_superadmin_headers

        actor = db.session.get(User, self.accounts["superadmin"]["id"])
        headers = clerk_superadmin_headers(actor)
        headers["X-Tenant"] = "acceptance-a"
        self.assertTrue(self.descriptor(headers)["can_manage"])
        response = self.client.put(self.url(template_slug="welcome"), json={"name": "Platform admin"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.persisted()["campaign_templates"][0]["name"], "Platform admin")
        self.assertEqual(self.persisted(self.tenant_b_id), self.other)

    def test_duplicate_create_and_unknown_update_leave_configuration_unchanged(self):
        headers = self.login()
        duplicate = self.client.post(self.url(), json={"slug": "welcome", "name": "Duplicate", "message": "Duplicate"}, headers=headers)
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())
        missing = self.client.put(self.url(template_slug="missing"), json={"name": "Missing"}, headers=headers)
        self.assertEqual(missing.status_code, 404, missing.get_json())
        self.assertEqual(self.persisted(), self.initial)


if __name__ == "__main__":
    unittest.main(verbosity=2)
