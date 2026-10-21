import os
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AnalyticsEvent, EncEncuesta, EncRespuesta, TenantProfile, TenantTicket, User
from services.analytics_service import analytics_service
from services.auth_session_lifecycle import issue_token


class V2AnalyticsTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2AnalyticsOverviewTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2AnalyticsTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(name="analytics-admin", email="analytics@test.com", rol="admin", tenant_slug="analytics-tenant")
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()
        self.tenant = TenantProfile(slug="analytics-tenant", nombre="Analytics Tenant", tipo="pyme", pyme_id=self.admin.id)
        db.session.add(self.tenant)
        db.session.commit()
        self.admin.tenant_id = self.tenant.id
        db.session.add(self.admin)
        db.session.add(
            TenantTicket(
                tenant_id=self.tenant.id,
                user_id=self.admin.id,
                categoria="soporte",
                descripcion="Ticket abierto",
                estado="nuevo",
                origen="web",
                datos_extra={"title": "Ticket abierto", "priority": "high"},
            )
        )
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self):
        token = issue_token(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "tenant_slug": self.admin.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
        )
        return {"Authorization": f"Bearer {token}"}

    def test_overview_returns_summary_contract(self):
        headers = {**self._auth(), "X-Tenant-Slug": self.tenant.slug, "X-Request-Id": "analytics-contract-1"}
        response = self.client.get("/api/v2/analytics/overview", headers=headers)

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        summary = payload.get("summary") or {}
        self.assertEqual(payload.get("contract_version"), "analytics.overview.v2")
        self.assertEqual(payload.get("request_id"), "analytics-contract-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "analytics-contract-1")
        self.assertIn("conversations", summary)
        self.assertEqual(summary.get("open_tickets"), 1)
        self.assertEqual(summary.get("nps"), 0)
        self.assertEqual(summary.get("csat"), 0)
        self.assertIn("handoff_rate", summary)
        self.assertIsNone(payload["survey_completion_rate"])
        self.assertIsNone(payload["survey_participation_rate"])
        self.assertEqual(payload["survey_participation_rate_metadata"]["reason_code"], "no_active_survey")
        self.assertEqual(payload["survey_completion_rate_metadata"]["reason_code"], "survey_completion_population_unavailable")
        self.assertIsNone(payload["survey_completion_rate_metadata"]["observed_response_records"])

    def _published_survey(self):
        survey = EncEncuesta(
            tenant_id=self.tenant.id,
            slug="survey-rate-evidence",
            titulo="Encuesta de prueba local",
            estado="publicada",
            tipo="opinion",
        )
        db.session.add(survey)
        db.session.flush()
        return survey

    def _response(self, survey, *, origin="real", index=0):
        db.session.add(EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=self.tenant.id,
            huella_unica=f"local-rate-{origin}-{index}",
            response_origin=origin,
            canal="web",
        ))

    def test_empty_published_survey_preserves_zero_count_without_inventing_zero_percent(self):
        survey = self._published_survey()
        db.session.commit()
        stats = analytics_service.get_survey_summary(self.tenant.id)["stats"]

        self.assertEqual(stats["total_votes"], 0)
        self.assertIsNone(stats["participation_rate"])
        evidence = stats["participation_rate_metadata"]
        self.assertEqual(evidence["observed_response_records"], 0)
        self.assertEqual(evidence["scope"]["survey_id"], survey.id)
        self.assertIsNone(evidence["numerator"]["value"])
        self.assertIsNone(evidence["denominator"]["value"])
        self.assertFalse(evidence["denominator"]["verified"])

    def test_real_response_count_without_activity_population_has_no_participation_or_completion_rate(self):
        survey = self._published_survey()
        for index in range(2):
            self._response(survey, index=index)
        self._response(survey, origin="synthetic_demo")
        self._response(survey, origin="legacy_unverified")
        db.session.commit()
        stats = analytics_service.get_survey_summary(self.tenant.id)["stats"]

        self.assertEqual(stats["total_votes"], 2)
        self.assertIsNone(stats["participation_rate"])
        self.assertEqual(stats["participation_rate_metadata"]["observed_response_records"], 2)
        self.assertEqual(stats["participation_rate_metadata"]["reason_code"], "survey_eligible_population_not_sealed")
        self.assertEqual(stats["response_provenance"]["synthetic_responses_excluded"], 1)
        self.assertEqual(stats["response_provenance"]["unverified_responses_excluded"], 1)

        response = self.client.get(
            "/api/v2/analytics/overview", headers={**self._auth(), "X-Tenant-Slug": self.tenant.slug},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["survey_response_count"], 2)
        self.assertIsNone(payload["survey_completion_rate"])
        self.assertIsNone(payload["survey_participation_rate"])
        self.assertIsNone(payload["survey_completion_rate_metadata"]["numerator"]["value"])
        self.assertIsNone(payload["survey_completion_rate_metadata"]["denominator"]["value"])
        self.assertEqual(payload["survey_completion_rate_metadata"]["observed_response_records"], 2)

    def test_recent_active_users_are_not_a_verified_survey_denominator(self):
        survey = self._published_survey()
        for index in range(5):
            self._response(survey, index=index)
        db.session.add(AnalyticsEvent(tenant_id=self.tenant.id, user_id=self.admin.id, event_type="message_in"))
        db.session.commit()
        stats = analytics_service.get_survey_summary(self.tenant.id)["stats"]

        self.assertEqual(stats["total_votes"], 5)
        self.assertIsNone(stats["participation_rate"])
        evidence = stats["participation_rate_metadata"]
        self.assertEqual(evidence["observed_response_records"], 5)
        self.assertEqual(evidence["numerator"]["grain"], "unique_eligible_respondents")
        self.assertFalse(evidence["numerator"]["verified"])
        self.assertEqual(evidence["denominator"]["grain"], "eligible_population")
        self.assertIsNone(evidence["denominator"]["value"])

    def test_simulated_responses_do_not_supply_real_rate_numerators(self):
        survey = self._published_survey()
        for index in range(3):
            self._response(survey, origin="synthetic_demo", index=index)
        db.session.commit()
        stats = analytics_service.get_survey_summary(self.tenant.id)["stats"]

        self.assertEqual(stats["total_votes"], 0)
        self.assertIsNone(stats["participation_rate"])
        self.assertEqual(stats["participation_rate_metadata"]["observed_response_records"], 0)
        self.assertIsNone(stats["participation_rate_metadata"]["numerator"]["value"])
        self.assertEqual(stats["response_provenance"]["synthetic_responses_excluded"], 3)


if __name__ == "__main__":
    unittest.main()
