import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    AnalyticsEvent, EncEncuesta, EncRespuesta, MunicipioTicket, PymeTicket,
    TenantProfile, TenantTicket, TicketSatisfaccion, User,
)
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

    def _auth(self, user=None):
        user = user if user is not None else self.admin
        token = issue_token(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
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
        self.assertEqual(payload["tenant_slug"], self.tenant.slug)
        self.assertEqual(payload.get("request_id"), "analytics-contract-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "analytics-contract-1")
        self.assertIn("conversations", summary)
        self.assertEqual(summary.get("open_tickets"), 1)
        self.assertIsNone(summary["nps"])
        self.assertIsNone(summary["csat"])
        self.assertIsNone(payload["nps_score"])
        self.assertIsNone(payload["csat_score"])
        satisfaction = payload["ticket_satisfaction"]
        self.assertEqual(satisfaction["contract_version"], "analytics.ticket_satisfaction.v1")
        self.assertEqual(satisfaction["state"], "observed")
        self.assertEqual(satisfaction["rated_ticket_count"], 0)
        self.assertEqual(satisfaction["basis"], "first_valid_rating_recorded_at")
        self.assertIs(satisfaction["scale_verified"], False)
        self.assertEqual(satisfaction["label"], "Casos calificados")
        self.assertTrue(satisfaction["detail"])
        self.assertIn("handoff_rate", summary)
        self.assertIsNone(payload["survey_completion_rate"])
        self.assertIsNone(payload["survey_participation_rate"])
        self.assertEqual(payload["survey_participation_rate_metadata"]["reason_code"], "no_active_survey")
        self.assertEqual(payload["survey_completion_rate_metadata"]["reason_code"], "survey_completion_population_unavailable")
        self.assertIsNone(payload["survey_completion_rate_metadata"]["observed_response_records"])

    def _satisfaction_overview(self, *, user=None, **query):
        user = user if user is not None else self.admin
        response = self.client.get(
            "/api/v2/analytics/overview", query_string=query,
            headers={**self._auth(user), "X-Tenant-Slug": user.tenant_slug},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["tenant_slug"], user.tenant_slug)
        return payload["ticket_satisfaction"]

    def _pyme_ticket(self, number, tenant_id, *, user_id=None):
        ticket = PymeTicket(
            nro_ticket=number, tenant_id=tenant_id, user_id=user_id, pregunta="Caso sintético",
            fecha=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        db.session.add(ticket)
        db.session.flush()
        return ticket

    def _rating(self, tipo, ticket_id, score, when):
        rating = TicketSatisfaccion(ticket_id=ticket_id, tipo=tipo, puntuacion=score, fecha=when)
        db.session.add(rating)
        db.session.flush()
        if when is None:
            rating.fecha = None

    def test_ticket_satisfaction_counts_first_valid_rating_in_rating_period(self):
        old = datetime(2026, 6, 10, tzinfo=timezone.utc)
        start = datetime(2026, 7, 1, 3, tzinfo=timezone.utc)
        recent = datetime(2026, 7, 10, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, 3, tzinfo=timezone.utc)
        own_ticket = self._pyme_ticket(1, self.tenant.id)
        repeated = self._pyme_ticket(2, self.tenant.id)
        invalid = self._pyme_ticket(3, self.tenant.id)
        undated = self._pyme_ticket(4, self.tenant.id)
        reverse_dates = self._pyme_ticket(5, self.tenant.id)
        self._rating("pyme", own_ticket.id, 0, start)
        self._rating("pyme", own_ticket.id, 10, end)
        self._rating("pyme", repeated.id, 9, old)
        self._rating("pyme", repeated.id, 9, recent)
        self._rating("pyme", invalid.id, 11, old)
        self._rating("pyme", invalid.id, 3.5, recent)
        self._rating("pyme", invalid.id, 5, end)
        self._rating("pyme", undated.id, 1, None)
        self._rating("pyme", undated.id, 1, recent)
        self._rating("pyme", reverse_dates.id, 6, recent)
        self._rating("pyme", reverse_dates.id, 7, old)
        db.session.commit()

        june = self._satisfaction_overview(**{"from": "2026-06-01", "to": "2026-06-30"})
        july = self._satisfaction_overview(**{"from": "2026-07-01T00:00:00-03:00", "to": "2026-07-31T00:00:00-03:00"})
        no_boundary = self._satisfaction_overview(**{"from": "2026-07-01T00:00:01-03:00", "to": "2026-07-30T23:59:59-03:00"})
        self.assertEqual((june["rated_ticket_count"], july["rated_ticket_count"], no_boundary["rated_ticket_count"]), (1, 3, 1))

    def test_ticket_satisfaction_preserves_tenant_and_type_boundaries(self):
        other_admin = User(name="other-admin", email="analytics-other@example.invalid", rol="admin", tenant_slug="other-tenant")
        other_admin.set_password("synthetic-secret-321")
        db.session.add(other_admin)
        db.session.flush()
        other = TenantProfile(slug="other-tenant", nombre="Other Tenant", tipo="municipio", municipio_id=other_admin.id)
        db.session.add(other)
        db.session.flush()
        other_admin.tenant_id = other.id
        business = self._pyme_ticket(1, self.tenant.id)
        unscoped = self._pyme_ticket(2, None, user_id=self.admin.id)
        foreign = self._pyme_ticket(3, other.id, user_id=self.admin.id)
        municipality = MunicipioTicket(
            id=business.id, tenant_id=other.id, municipio_id=other_admin.id, pregunta="Caso municipal sintético",
        )
        legacy_municipality = MunicipioTicket(municipio_id=other_admin.id, pregunta="Caso municipal legado sintético")
        db.session.add_all([municipality, legacy_municipality])
        db.session.flush()
        when = datetime(2026, 7, 10, tzinfo=timezone.utc)
        self._rating("pyme", business.id, 9, when)
        self._rating("pyme", unscoped.id, 9, when)
        self._rating("pyme", foreign.id, 9, when)
        self._rating("pyme", 400, 9, when)
        self._rating("municipio", municipality.id, 0, when)
        self._rating("municipio", legacy_municipality.id, 4, when)
        db.session.commit()

        period = {"from": "2026-07-01", "to": "2026-07-31"}
        self.assertEqual(self._satisfaction_overview(**period)["rated_ticket_count"], 1)
        self.assertEqual(self._satisfaction_overview(user=other_admin, **period)["rated_ticket_count"], 2)
        db.session.add(TenantProfile(slug="ambiguous-other", nombre="Otro perfil", tipo="municipio", municipio_id=other_admin.id))
        db.session.commit()
        self.assertEqual(self._satisfaction_overview(user=other_admin, **period)["rated_ticket_count"], 1)

    def test_ticket_satisfaction_reports_channel_subset_as_unavailable(self):
        satisfaction = self._satisfaction_overview(channel="whatsapp")
        self.assertEqual(satisfaction["state"], "unavailable")
        self.assertIsNone(satisfaction["rated_ticket_count"])
        self.assertEqual(satisfaction["reason_code"], "rating_channel_unavailable")

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
