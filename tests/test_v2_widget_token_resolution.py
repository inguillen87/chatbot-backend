"""resolve_tenant_v2 selects a tenant by widget token only on an exact, unambiguous match."""

import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


class _Config(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2WidgetTokenResolutionTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_Config)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _tenant(self, slug, tokens, *, active=True):
        owner = User(email=f"owner-{slug}@test.com", name=f"Owner {slug}", rol="admin", tipo_chat="pyme")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug=slug, nombre=f"Tenant {slug}", tipo="pyme", plan="full", pyme_id=owner.id,
            configuracion={"widget_tokens": tokens}, is_active=active,
        )
        db.session.add(tenant)
        db.session.commit()
        return tenant

    def _current(self, token):
        return self.client.get("/api/v2/tenants/current", headers={"X-Widget-Token": token})

    def test_exact_widget_token_selects_its_tenant(self):
        self._tenant("v2-exact", ["v2-token-exact"])

        response = self._current("v2-token-exact")

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["tenant"]["slug"], "v2-exact")

    def test_substring_of_another_tenant_token_selects_nothing(self):
        self._tenant("v2-demo-production", ["demo-production"])

        response = self._current("demo")

        self.assertEqual(response.status_code, 404, response.get_json())

    def test_ambiguous_widget_token_selects_nothing(self):
        self._tenant("v2-ambiguous-a", ["shared-token"])
        self._tenant("v2-ambiguous-b", ["shared-token"])

        response = self._current("shared-token")

        self.assertEqual(response.status_code, 404, response.get_json())

    def test_inactive_tenant_is_not_selected_by_widget_token(self):
        self._tenant("v2-inactive", ["inactive-token"], active=False)

        response = self._current("inactive-token")

        self.assertEqual(response.status_code, 404, response.get_json())

    def test_explicit_slug_still_wins_over_token(self):
        self._tenant("v2-slug-a", ["token-a"])
        self._tenant("v2-slug-b", ["token-b"])

        response = self.client.get(
            "/api/v2/tenants/current", headers={"X-Tenant-Slug": "v2-slug-a", "X-Widget-Token": "token-b"}
        )

        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["tenant"]["slug"], "v2-slug-a")


if __name__ == "__main__":
    unittest.main()
