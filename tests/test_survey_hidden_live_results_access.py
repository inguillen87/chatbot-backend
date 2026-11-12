"""Offline access regression; routing selectors never grant hidden-result access."""
from datetime import datetime, timedelta, timezone
import unittest

from app import create_app
from config import TestConfig
from models import db, EncEncuesta, TenantProfile, User
from services.auth_session_lifecycle import issue_token


class HiddenSurveyLiveResultsAccessTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.client = self.app.test_client()
        self.owner = User(name="Owner", email="hidden-owner@example.test", rol="admin", password_hash="local-fixture", tenant_slug="hidden-owner")
        self.foreign = User(name="Foreign", email="hidden-foreign@example.test", rol="admin", password_hash="local-fixture", tenant_slug="hidden-foreign")
        db.session.add_all([self.owner, self.foreign])
        db.session.flush()
        self.tenant = TenantProfile(slug="hidden-owner", nombre="Owner", tipo="pyme", pyme_id=self.owner.id)
        self.foreign_tenant = TenantProfile(slug="hidden-foreign", nombre="Foreign", tipo="pyme", pyme_id=self.foreign.id)
        db.session.add_all([self.tenant, self.foreign_tenant])
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.foreign.tenant_id = self.foreign_tenant.id
        self.viewer = User(name="Viewer", email="hidden-viewer@example.test", rol="usuario", password_hash="local-fixture", tenant_id=self.tenant.id, tenant_slug=self.tenant.slug)
        self.survey = EncEncuesta(tenant_id=self.tenant.id, slug="hidden-survey", titulo="Private results", tipo="opinion", estado="publicada", mostrar_resultados_envivo=False, permitir_comentarios=True)
        db.session.add_all([self.viewer, self.survey])
        db.session.commit()
        self.url = f"/api/v2/public/surveys/{self.survey.slug}/live-results?tenant_slug={self.tenant.slug}"

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def authorization(self, user):
        token = issue_token({"user_id": user.id, "rol": user.rol, "tenant_slug": user.tenant_slug, "exp": datetime.now(timezone.utc) + timedelta(hours=1)})
        return {"Authorization": f"Bearer {token}"}

    def test_public_tenant_selector_does_not_grant_owner_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "live_results_hidden")

    def test_foreign_admin_and_same_tenant_citizen_cannot_read_hidden_results(self):
        for actor in (self.foreign, self.viewer):
            with self.subTest(actor=actor.name):
                response = self.client.get(self.url, headers=self.authorization(actor))
                self.assertEqual(response.status_code, 403, response.get_json())
                self.assertEqual(response.get_json()["reason_code"], "live_results_hidden")

    def test_authenticated_owner_uses_admin_analytics_and_cannot_read_hidden_public_results(self):
        response = self.client.get(self.url, headers=self.authorization(self.owner))
        self.assertEqual(response.status_code, 403, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "live_results_hidden")
        response = self.client.get(f"/api/v2/surveys/{self.survey.id}/analytics?tenant_slug={self.tenant.slug}", headers=self.authorization(self.owner))
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_published_results_remain_public_and_comment_config_matches_v1(self):
        self.survey.mostrar_resultados_envivo = True
        db.session.commit()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.headers["Cache-Control"].startswith("public"))
        payload = self.client.get(f"/api/v2/public/surveys/{self.survey.slug}?tenant_slug={self.tenant.slug}").get_json()
        self.assertEqual(payload["commentConfig"]["acceptedModes"], ["anon"])
        self.assertEqual(payload["commentConfig"]["socialProviders"], [])
        self.assertTrue(payload["commentConfig"]["requiresSocialToken"])
