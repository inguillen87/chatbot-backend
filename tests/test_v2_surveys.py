import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import EncRespuesta, TenantProfile, User
from routes.v2.surveys import _public_response_rate_buckets
from services.demo_surveys import (
    build_demo_public_survey_payload,
    build_demo_survey_response_ack,
    build_demo_surveys_votings_contract,
)


class V2SurveysTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class V2SurveysApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2SurveysTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin_1 = self._create_user("admin-survey-a@test.com", "admin", "tenant-surv-a")
        self.tenant_1 = TenantProfile(
            slug="tenant-surv-a",
            nombre="Tenant Survey A",
            tipo="municipio",
            pyme_id=self.admin_1.id,
            plan="full",
        )
        db.session.add(self.tenant_1)
        db.session.commit()
        self.admin_1.tenant_id = self.tenant_1.id
        db.session.add(self.admin_1)

        self.admin_2 = self._create_user("admin-survey-b@test.com", "admin", "tenant-surv-b")
        self.tenant_2 = TenantProfile(
            slug="tenant-surv-b",
            nombre="Tenant Survey B",
            tipo="pyme",
            pyme_id=self.admin_2.id,
            plan="full",
        )
        db.session.add(self.tenant_2)
        db.session.commit()
        self.admin_2.tenant_id = self.tenant_2.id
        db.session.add(self.admin_2)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _create_user(self, email: str, rol: str, tenant_slug: str):
        user = User(name=email.split("@")[0], email=email, rol=rol, tenant_slug=tenant_slug)
        user.set_password("secret123")
        db.session.add(user)
        db.session.flush()
        return user

    def _auth(self, user: User):
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": user.tenant_slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}

    def _create_payload(self):
        now = datetime.utcnow()
        return {
            "title": "Encuesta de satisfacción",
            "description": "Qué te pareció el servicio",
            "channel": "web",
            "live_vote": True,
            "show_live_results": True,
            "opens_at": (now - timedelta(days=1)).isoformat() + "Z",
            "closes_at": (now + timedelta(days=7)).isoformat() + "Z",
            "questions": [
                {
                    "type": "single",
                    "label": "¿Cómo calificás la atención?",
                    "required": True,
                    "options": ["Excelente", "Buena", "Regular"],
                    "order_index": 1,
                }
            ],
        }

    def test_admin_can_create_publish_and_public_respond(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        create_resp = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers)
        self.assertEqual(create_resp.status_code, 201)
        survey_id = create_resp.get_json()["id"]

        publish_resp = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers)
        self.assertEqual(publish_resp.status_code, 200)
        publish_payload = publish_resp.get_json()
        token = publish_payload.get("public_token")
        self.assertTrue(token)
        self.assertEqual(publish_payload.get("public_state", {}).get("status"), "live")
        self.assertEqual(publish_payload.get("realtime", {}).get("room"), f"encuesta:{self.tenant_1.slug}:{token}")
        self.assertEqual(publish_payload.get("realtime", {}).get("legacy_room"), f"encuesta_{token}")
        self.assertIn(f"encuesta_{token}", publish_payload.get("realtime", {}).get("rooms", []))
        self.assertEqual(
            publish_payload.get("realtime", {}).get("socket", {}).get("events", [])[0].get("name"),
            "survey_update_v2",
        )
        self.assertEqual(
            publish_payload.get("links", {}).get("qr_endpoint"),
            f"/api/public/encuestas/v1/{token}/qr?size=320",
        )
        self.assertIn("download_qr", [item.get("id") for item in publish_payload.get("next_steps", [])])

        public_get = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public_get.status_code, 200)
        public_payload = public_get.get_json()
        self.assertEqual(public_payload.get("contract_version"), "surveys.public.v2")
        self.assertEqual(public_payload.get("public_state", {}).get("status"), "live")
        self.assertEqual(public_payload.get("security", {}).get("surface"), "survey_public_response")
        self.assertEqual(public_payload.get("security", {}).get("status"), "not_required")
        self.assertEqual(
            public_payload.get("frontend_contract", {}).get("turnstile", {}).get("token_header"),
            "X-Turnstile-Token",
        )
        self.assertEqual(
            public_payload.get("links", {}).get("respond_endpoint"),
            f"/api/v2/public/surveys/{token}/respond",
        )
        self.assertEqual(public_payload.get("share", {}).get("qr", {}).get("size"), 320)

        question_id = public_payload.get("preguntas", [])[0].get("id")
        option_id = public_payload.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_payload = {
            "anon_id": "anon-survey-1",
            "source": "web",
            "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
        }
        respond_resp = self.client.post(f"/api/v2/public/surveys/{token}/respond", json=respond_payload)
        self.assertEqual(respond_resp.status_code, 201)
        ack = respond_resp.get_json()
        self.assertTrue(ack.get("ok"))
        self.assertEqual(ack.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(ack.get("live_results_url"), f"/api/v2/public/surveys/{token}/live-results")
        self.assertEqual(ack.get("realtime", {}).get("room"), f"encuesta_{token}")
        self.assertEqual(
            ack.get("realtime", {}).get("polling", {}).get("href"),
            f"/api/v2/public/surveys/{token}/live-results",
        )
        self.assertEqual(
            ack.get("links", {}).get("qr_endpoint"),
            f"/api/public/encuestas/v1/{token}/qr?size=320",
        )
        self.assertIn("share_public_link", [item.get("id") for item in ack.get("next_steps", [])])

        live_resp = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0")
        self.assertEqual(live_resp.status_code, 200)
        live_payload = live_resp.get_json()
        self.assertEqual(live_payload.get("contract_version"), "surveys.live_results.v2")
        self.assertEqual(live_payload.get("total_respuestas"), 1)
        self.assertEqual(live_payload.get("render_contract", {}).get("preferred_visualization"), "live_vote_command_center")
        self.assertFalse(live_payload.get("heatmap", {}).get("enabled"))
        first_question = live_payload.get("preguntas", [])[0]
        self.assertEqual(first_question.get("total_votos"), 1)
        self.assertEqual(first_question.get("opciones", [])[0].get("texto"), "Excelente")
        self.assertEqual(first_question.get("opciones", [])[0].get("votos"), 1)
        self.assertEqual(live_payload.get("timeline_minute", [])[0].get("respuestas"), 1)
        self.assertIn("ai_summary", live_payload.get("render_contract", {}).get("supports", []))
        self.assertIn("realtime_socket", live_payload.get("render_contract", {}).get("supports", []))
        self.assertIn("qr_share", live_payload.get("render_contract", {}).get("supports", []))
        self.assertEqual(live_payload.get("realtime", {}).get("room"), f"encuesta_{token}")
        self.assertEqual(
            live_payload.get("realtime", {}).get("versioning", {}).get("result_version"),
            live_payload.get("result_version"),
        )
        self.assertFalse(live_payload.get("empty_state", {}).get("is_empty"))
        self.assertTrue(live_payload.get("live_telemetry", {}).get("has_responses"))

    def test_v2_public_live_results_hidden_when_not_enabled(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        payload = self._create_payload()
        payload["show_live_results"] = False

        survey_id = self.client.post("/api/v2/surveys", json=payload, headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_resp = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={
                "anon_id": "anon-hidden-1",
                "source": "web",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
        )
        self.assertEqual(respond_resp.status_code, 201)
        self.assertNotIn("live_results_url", respond_resp.get_json())

        live_resp = self.client.get(f"/api/v2/public/surveys/{token}/live-results")
        self.assertEqual(live_resp.status_code, 403)
        self.assertEqual(live_resp.get_json().get("reason_code"), "live_results_hidden")

    def test_v2_public_response_requires_identity_when_anonymous_is_disabled(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        payload = self._create_payload()
        payload["allow_anonymous"] = False
        payload["uniqueness_policy"] = "por_dni"

        survey_id = self.client.post("/api/v2/surveys", json=payload, headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")
        answer = {"respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}]}

        missing_identity = self.client.post(f"/api/v2/public/surveys/{token}/respond", json=answer)
        self.assertEqual(missing_identity.status_code, 400, missing_identity.get_json())
        missing_payload = missing_identity.get_json()
        self.assertEqual(missing_payload.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(missing_payload.get("reason_code"), "identity_required")
        self.assertEqual(missing_payload.get("action_hint"), "provide_identity")
        self.assertEqual(missing_payload.get("required_identity"), ["dni"])

        identified = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "dni": "32877851"},
        )
        self.assertEqual(identified.status_code, 201, identified.get_json())

        duplicate = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "dni": "32877851"},
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())

    def test_v2_public_response_live_action_preserves_tenant_slug(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}?tenant_slug={self.tenant_1.slug}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_resp = self.client.post(
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_1.slug}",
            json={
                "anon_id": "anon-tenant-scoped-1",
                "source": "web",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
        )

        self.assertEqual(respond_resp.status_code, 201, respond_resp.get_json())
        ack = respond_resp.get_json()
        expected_live_url = f"/api/v2/public/surveys/{token}/live-results?tenant_slug={self.tenant_1.slug}"
        self.assertEqual(ack.get("live_results_url"), expected_live_url)
        self.assertEqual(ack.get("ui_actions", [])[0].get("href"), expected_live_url)
        self.assertEqual(ack.get("links", {}).get("live_results_endpoint"), expected_live_url)
        self.assertEqual(ack.get("realtime", {}).get("polling", {}).get("href"), expected_live_url)

        live_resp = self.client.get(f"{expected_live_url}&include_heatmap=0")
        self.assertEqual(live_resp.status_code, 200, live_resp.get_json())
        self.assertEqual(live_resp.get_json().get("total_respuestas"), 1)
        self.assertEqual(live_resp.get_json().get("links", {}).get("live_results_endpoint"), expected_live_url)

    def test_v2_public_response_rate_limit_returns_contract_and_headers(self):
        self.app.config["PUBLIC_ENCUESTAS_RATE_LIMIT"] = 1
        self.app.config["PUBLIC_ENCUESTAS_RATE_PERIOD"] = 60
        _public_response_rate_buckets.clear()

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        first_resp = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={
                "anon_id": "anon-rate-1",
                "source": "web",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
            headers={"X-Forwarded-For": "198.51.100.20", "X-Request-Id": "survey-rate-1"},
        )
        self.assertEqual(first_resp.status_code, 201, first_resp.get_json())
        self.assertEqual(first_resp.headers.get("X-RateLimit-Limit"), "1")
        self.assertEqual(first_resp.headers.get("X-RateLimit-Remaining"), "0")
        self.assertEqual(first_resp.get_json().get("request_id"), "survey-rate-1")

        limited_resp = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={
                "anon_id": "anon-rate-2",
                "source": "web",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
            headers={"X-Forwarded-For": "198.51.100.20", "X-Request-Id": "survey-rate-2"},
        )
        self.assertEqual(limited_resp.status_code, 429, limited_resp.get_json())
        payload = limited_resp.get_json()
        self.assertEqual(payload.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(payload.get("reason_code"), "rate_limited")
        self.assertEqual(payload.get("request_id"), "survey-rate-2")
        self.assertEqual(limited_resp.headers.get("X-RateLimit-Remaining"), "0")
        self.assertTrue(limited_resp.headers.get("Retry-After"))
        self.assertGreaterEqual(payload.get("rate_limit", {}).get("retry_after_seconds"), 1)

    def test_v2_public_response_rejects_invalid_turnstile_when_enforced(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        _public_response_rate_buckets.clear()

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch("routes.v2.surveys.verify_turnstile", return_value=False) as verify_turnstile_mock:
            response = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json={
                    "anon_id": "anon-turnstile-invalid",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                headers={"X-Forwarded-For": "198.51.100.77"},
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(payload.get("reason_code"), "turnstile_verificacion_fallida")
        self.assertEqual(payload.get("security", {}).get("surface"), "survey_public_response")
        self.assertEqual(payload.get("security", {}).get("status"), "verification_failed")
        self.assertTrue(payload.get("frontend_contract", {}).get("reset_turnstile"))
        self.assertTrue(payload.get("frontend_contract", {}).get("can_retry"))
        self.assertEqual(EncRespuesta.query.count(), 0)
        verify_turnstile_mock.assert_called_once()

    def test_v2_public_response_fails_closed_when_turnstile_enforced_without_secret(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = ""
        self.app.config["TURNSTILE_SECRET_KEY"] = ""
        _public_response_rate_buckets.clear()

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch.dict(os.environ, {"CLOUDFLARE_TURNSTILE_SECRET_KEY": "", "TURNSTILE_SECRET_KEY": ""}):
            with patch("routes.v2.surveys.verify_turnstile", return_value=True) as verify_turnstile_mock:
                response = self.client.post(
                    f"/api/v2/public/surveys/{token}/respond",
                    json={
                        "anon_id": "anon-turnstile-missing-secret",
                        "source": "web",
                        "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                    },
                )

        self.assertEqual(response.status_code, 503, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("reason_code"), "turnstile_no_configurado")
        self.assertEqual(payload.get("security", {}).get("status"), "misconfigured")
        self.assertFalse(payload.get("security", {}).get("configured"))
        self.assertFalse(payload.get("frontend_contract", {}).get("can_retry"))
        self.assertEqual(EncRespuesta.query.count(), 0)
        verify_turnstile_mock.assert_not_called()

    def test_v2_public_response_accepts_valid_turnstile_token_when_enforced(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        _public_response_rate_buckets.clear()

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch("routes.v2.surveys.verify_turnstile", return_value=True) as verify_turnstile_mock:
            response = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json={
                    "anon_id": "anon-turnstile-valid",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                headers={"X-Turnstile-Token": "valid-turnstile-token"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("security", {}).get("status"), "verified")
        self.assertFalse(payload.get("frontend_contract", {}).get("reset_turnstile"))
        self.assertEqual(EncRespuesta.query.count(), 1)
        verify_turnstile_mock.assert_called_once()

    def test_survey_draft_accepts_incomplete_payload(self):
        headers = {
            **self._auth(self.admin_1),
            "X-Tenant-Slug": self.tenant_1.slug,
            "X-Request-Id": "draft-contract-1",
            "Idempotency-Key": "draft-offline-1",
        }
        response = self.client.post(
            "/api/v2/surveys/draft",
            json={
                "title": "Borrador offline",
                "description": "",
                "questions": [{"id": "question-1", "title": "", "type": "single"}],
            },
            headers=headers,
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("contract_version"), "surveys.draft.v2")
        self.assertEqual(payload.get("request_id"), "draft-contract-1")
        self.assertEqual(payload.get("status"), "draft")
        self.assertEqual(payload.get("draft_id"), "draft_draft-offline-1")
        self.assertEqual(payload.get("idempotency_key"), "draft-offline-1")

    def test_free_plan_cannot_create_or_save_survey_draft(self):
        self.tenant_1.plan = "free"
        db.session.add(self.tenant_1)
        db.session.commit()
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        create_resp = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers)
        self.assertEqual(create_resp.status_code, 403)
        create_payload = create_resp.get_json()
        self.assertEqual(create_payload["error"], "plan_required")
        self.assertEqual(create_payload["feature"]["id"], "surveys_votings")
        self.assertFalse(create_payload["access"]["features"]["surveys_votings"]["enabled"])

        draft_resp = self.client.post(
            "/api/v2/surveys/draft",
            json={"title": "Borrador bloqueado", "questions": []},
            headers=headers,
        )
        self.assertEqual(draft_resp.status_code, 403)
        self.assertEqual(draft_resp.get_json()["error"], "plan_required")

    def test_closed_survey_rejects_public_responses(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        created = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()
        survey_id = created["id"]
        publish = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()
        token = publish["public_token"]

        close_resp = self.client.post(f"/api/v2/surveys/{survey_id}/close", headers=headers)
        self.assertEqual(close_resp.status_code, 200)

        failed_resp = self.client.post(f"/api/v2/public/surveys/{token}/respond", json={"respuestas": []})
        self.assertEqual(failed_resp.status_code, 403)


    def test_analytics_surveys_returns_votes_for_tenant(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")
        self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={"anon_id": "a-analytics-1", "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}]},
        )

        analytics_resp = self.client.get("/api/v2/analytics/surveys", headers=headers)
        self.assertEqual(analytics_resp.status_code, 200)
        stats = (analytics_resp.get_json() or {}).get("stats") or {}
        self.assertGreaterEqual(int(stats.get("total_votes") or 0), 1)

    def test_tenant_isolation_on_list(self):
        headers_1 = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        headers_2 = {**self._auth(self.admin_2), "X-Tenant-Slug": self.tenant_2.slug}

        self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers_1)
        self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers_2)

        listed = self.client.get("/api/v2/surveys", headers=headers_1)
        self.assertEqual(listed.status_code, 200)
        payload = listed.get_json()
        self.assertTrue(payload["access"]["features"]["surveys_votings"]["enabled"])
        items = payload.get("items") or []
        self.assertTrue(items)
        self.assertTrue(all(item.get("tenant_id") == self.tenant_1.id for item in items))

    def test_demo_survey_contract_exposes_qr_share_and_polling_realtime(self):
        contract = build_demo_surveys_votings_contract(
            sector="gobierno",
            tenant_slug="demo-survey-v2",
            public_base_url="https://demo.chatboc.test",
        )
        item = contract["items"][0]
        slug = item["slug"]

        self.assertEqual(item["links"]["qr_endpoint"], f"/api/public/encuestas/v1/{slug}/qr?size=320")
        self.assertEqual(item["share"]["qr"]["target_url"], f"https://demo.chatboc.test/e/{slug}")
        self.assertFalse(item["realtime"]["socket"]["enabled"])
        self.assertEqual(item["realtime"]["polling"]["href"], f"/api/public/encuestas/v1/{slug}/live-results")
        self.assertIn("download_qr", [step["id"] for step in item["next_steps"]])

        public_payload = build_demo_public_survey_payload(slug, public_base_url="https://demo.chatboc.test")
        self.assertEqual(public_payload["public_state"]["status"], "live")
        self.assertEqual(public_payload["resultados_envivo"]["realtime"]["polling"]["href"], item["live_results_endpoint"])

        ack = build_demo_survey_response_ack(
            slug,
            {"respuestas": [{"opcion": public_payload["preguntas"][0]["opciones"][0]["texto"]}]},
            public_base_url="https://demo.chatboc.test",
        )
        self.assertTrue(ack["accepted"])
        self.assertEqual(ack["links"]["qr_endpoint"], item["links"]["qr_endpoint"])
        self.assertEqual(ack["realtime"]["transports"], ["polling"])


if __name__ == "__main__":
    unittest.main()
