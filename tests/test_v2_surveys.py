import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt
from sqlalchemy import event

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    AnalyticsEventV2,
    EncEncuesta,
    EncRespuesta,
    PointsTransaction,
    SurveyDraft,
    SurveyDraftIdempotency,
    SurveyResponseEffect,
    SurveyResponseReceipt,
    TenantProfile,
    User,
)
from socket_service import _is_authorized_survey_room, emit_survey_update
from routes.v2.surveys import _survey_response_count
from services.demo_surveys import (
    build_demo_public_survey_payload,
    build_demo_survey_response_ack,
    build_demo_surveys_votings_contract,
)
from services.encuestas_service import (
    EncuestaError,
    emit_survey_response_update,
    get_public_encuesta,
)
from services.survey_response_provenance import (
    SURVEY_DEMO_SEEDING_CONTRACT_VERSION,
    is_trusted_demo_seed_response,
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
        self._survey_submission_sequence = 0

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

    def _post_public_response(
        self,
        endpoint,
        *,
        json,
        headers=None,
        client=None,
        submission_id=None,
        **kwargs,
    ):
        """Submit through the production idempotency contract explicitly.

        Every invocation gets a fresh deterministic key unless the test owns a
        specific key already. Tests exercising exact replay continue to call
        the client directly with the same key twice.
        """

        self._survey_submission_sequence += 1
        body = dict(json or {})
        request_headers = dict(headers or {})
        body_key = next(
            (
                body.get(field)
                for field in (
                    "submission_id",
                    "submissionId",
                    "idempotency_key",
                    "idempotencyKey",
                )
                if body.get(field)
            ),
            None,
        )
        key = (
            submission_id
            or body_key
            or request_headers.get("Idempotency-Key")
            or f"test-survey-response-{self._survey_submission_sequence:04d}"
        )
        body.setdefault("submission_id", key)
        request_headers.setdefault("Idempotency-Key", key)
        target_client = client or self.client
        return target_client.post(
            endpoint,
            json=body,
            headers=request_headers,
            **kwargs,
        )

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

    def _create_published_answer_context(self, payload):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        create_resp = self.client.post("/api/v2/surveys", json=payload, headers=headers)
        self.assertEqual(create_resp.status_code, 201, create_resp.get_json())
        survey_id = create_resp.get_json()["id"]

        publish_resp = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers)
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        token = publish_resp.get_json()["public_token"]

        public_resp = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public_resp.status_code, 200, public_resp.get_json())
        public_payload = public_resp.get_json()
        question_id = public_payload["preguntas"][0]["id"]
        option_id = public_payload["preguntas"][0]["opciones"][0]["id"]
        answer = {"respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}]}
        return headers, survey_id, token, public_payload, answer

    def _public_response_alias_paths(self, token, *, tenant_slug=None):
        tenant_slug = tenant_slug or self.tenant_1.slug
        tenant_query = f"tenant_slug={tenant_slug}"
        return (
            f"/api/v2/public/surveys/{token}/respond?{tenant_query}",
            f"/api/public/encuestas/{token}/responder?{tenant_query}",
            f"/api/public/encuestas/v1/{token}/responder?{tenant_query}",
            f"/api/public/encuestas/{token}/respuestas?{tenant_query}",
            f"/api/public/encuestas/v1/{token}/respuestas?{tenant_query}",
            f"/public/encuestas/{token}/responder?{tenant_query}",
            f"/public/encuestas/v1/{token}/responder?{tenant_query}",
            f"/public/encuestas/{token}/respuestas?{tenant_query}",
            f"/public/encuestas/v1/{token}/respuestas?{tenant_query}",
            f"/api/pwa/public/surveys/{token}/respond?tenant={tenant_slug}",
        )

    def test_legacy_entity_id_collision_cannot_cross_tenant_boundary(self):
        # These legacy IDs inhabit the User table namespace and must never be
        # interpreted as TenantProfile IDs.
        self.admin_1.municipio_id = self.tenant_2.id
        self.admin_1.pyme_id = self.tenant_2.id
        db.session.commit()

        cross_tenant_headers = {
            **self._auth(self.admin_1),
            "X-Tenant-Slug": self.tenant_2.slug,
        }
        denied_list = self.client.get("/api/v2/surveys", headers=cross_tenant_headers)
        self.assertIn(denied_list.status_code, {403, 404}, denied_list.get_json())

        denied_create = self.client.post(
            "/api/v2/surveys",
            json=self._create_payload(),
            headers=cross_tenant_headers,
        )
        self.assertIn(denied_create.status_code, {403, 404}, denied_create.get_json())
        self.assertEqual(EncEncuesta.query.filter_by(tenant_id=self.tenant_2.id).count(), 0)

        own_headers = {
            **self._auth(self.admin_1),
            "X-Tenant-Slug": self.tenant_1.slug,
        }
        allowed_create = self.client.post(
            "/api/v2/surveys",
            json=self._create_payload(),
            headers=own_headers,
        )
        self.assertEqual(allowed_create.status_code, 201, allowed_create.get_json())
        self.assertEqual(allowed_create.get_json()["tenant_id"], self.tenant_1.id)

    def test_authorized_superadmin_persists_explicitly_resolved_tenant(self):
        superadmin = self._create_user(
            "survey-superadmin@test.com",
            "super_admin",
            self.tenant_1.slug,
        )
        superadmin.tenant_id = self.tenant_1.id
        db.session.commit()

        headers = {
            **self._auth(superadmin),
            "X-Tenant-Slug": self.tenant_2.slug,
        }
        # Production superadmins authenticate through Clerk; this test isolates
        # tenant resolution after that authentication gate has succeeded.
        with patch("utils.auth_helpers.user_from_token", return_value=superadmin):
            response = self.client.post(
                "/api/v2/surveys",
                json=self._create_payload(),
                headers=headers,
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        survey_id = response.get_json()["id"]
        self.assertEqual(response.get_json()["tenant_id"], self.tenant_2.id)
        self.assertEqual(db.session.get(EncEncuesta, survey_id).tenant_id, self.tenant_2.id)

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
        expected_room = f"encuesta:{self.tenant_1.slug}:{token}"
        self.assertEqual(publish_payload.get("realtime", {}).get("room"), expected_room)
        self.assertEqual(publish_payload.get("realtime", {}).get("legacy_room"), expected_room)
        self.assertEqual(publish_payload.get("realtime", {}).get("rooms"), [expected_room])
        event_names = {
            event.get("name")
            for event in publish_payload.get("realtime", {}).get("socket", {}).get("events", [])
            if isinstance(event, dict)
        }
        self.assertIn("survey_update_v2", event_names)
        self.assertIn("survey.vote.created", event_names)
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
            public_payload.get("frontend_contract", {}).get("idempotency", {}).get("body_field"),
            "submission_id",
        )
        self.assertTrue(
            public_payload.get("frontend_contract", {})
            .get("idempotency", {})
            .get("required_for_exactly_once")
        )
        self.assertEqual(
            public_payload.get("links", {}).get("respond_endpoint"),
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_1.slug}",
        )
        self.assertEqual(public_payload.get("share", {}).get("qr", {}).get("size"), 320)

        question_id = public_payload.get("preguntas", [])[0].get("id")
        option_id = public_payload.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_payload = {
            "anon_id": "anon-survey-1",
            "source": "web",
            "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
        }
        respond_resp = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json=respond_payload,
        )
        self.assertEqual(respond_resp.status_code, 201)
        ack = respond_resp.get_json()
        self.assertTrue(ack.get("ok"))
        self.assertEqual(ack.get("contract_version"), "surveys.public_response.v2")
        expected_live_url = f"/api/v2/public/surveys/{token}/live-results?tenant_slug={self.tenant_1.slug}"
        self.assertEqual(ack.get("live_results_url"), expected_live_url)
        self.assertEqual(ack.get("realtime", {}).get("room"), expected_room)
        self.assertEqual(
            ack.get("realtime", {}).get("polling", {}).get("href"),
            expected_live_url,
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
        self.assertEqual(live_payload.get("realtime", {}).get("room"), expected_room)
        self.assertEqual(
            live_payload.get("realtime", {}).get("versioning", {}).get("result_version"),
            live_payload.get("result_version"),
        )
        self.assertFalse(live_payload.get("empty_state", {}).get("is_empty"))
        self.assertTrue(live_payload.get("live_telemetry", {}).get("has_responses"))

    def test_public_demo_and_synthetic_metadata_aliases_remain_real(self):
        headers, survey_id, token, _public_payload, answer = (
            self._create_published_answer_context(self._create_payload())
        )
        answer.update(
            {
                "anon_id": "public-aliases-remain-real",
                "metadata": {"demo": True, "synthetic": True},
            }
        )

        response = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json=answer,
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        real_response = EncRespuesta.query.filter_by(encuesta_id=survey_id).one()
        self.assertEqual(
            real_response.metadata_payload,
            {"demo": True, "synthetic": True},
        )
        self.assertEqual(real_response.response_origin, "real")
        self.assertFalse(is_trusted_demo_seed_response(real_response))

        smuggling_answer = {
            **answer,
            "anon_id": "public-origin-smuggling-rejected",
            "metadata": {
                "is_demo_seed": True,
                "demo_seed_contract_version": (
                    SURVEY_DEMO_SEEDING_CONTRACT_VERSION
                ),
                "demo_batch_id": (
                    f"seed-{survey_id}-1755680400000-abcdef123456"
                ),
            },
        }
        smuggling_response = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json=smuggling_answer,
        )
        self.assertEqual(
            smuggling_response.status_code,
            400,
            smuggling_response.get_json(),
        )
        self.assertEqual(
            smuggling_response.get_json().get("reason_code"),
            "survey_demo_seed_metadata_reserved",
        )
        self.assertEqual(
            EncRespuesta.query.filter_by(encuesta_id=survey_id).count(),
            1,
        )

        db.session.add(
            EncRespuesta(
                encuesta_id=survey_id,
                tenant_id=self.tenant_1.id,
                response_origin="synthetic_demo",
                metadata_payload={
                    "is_demo_seed": True,
                    "demo_seed_contract_version": (
                        SURVEY_DEMO_SEEDING_CONTRACT_VERSION
                    ),
                    "demo_batch_id": (
                        f"seed-{survey_id}-1755680400000-abcdef123456"
                    ),
                },
                submitted_at=datetime.utcnow(),
            )
        )
        db.session.commit()

        statements = []

        def _capture_sql(_conn, _cursor, statement, _params, _context, _many):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", _capture_sql)
        try:
            with patch.object(
                type(EncRespuesta.query),
                "all",
                side_effect=AssertionError(
                    "response count must aggregate, not call all()"
                ),
            ), patch.object(
                type(EncRespuesta.query),
                "yield_per",
                side_effect=AssertionError(
                    "response count must not scan ORM rows"
                ),
            ):
                self.assertEqual(
                    _survey_response_count(db.session.get(EncEncuesta, survey_id)),
                    1,
                )
        finally:
            event.remove(db.engine, "before_cursor_execute", _capture_sql)

        count_statements = [
            statement.lower()
            for statement in statements
            if "count(" in statement.lower()
        ]
        self.assertTrue(count_statements, statements)
        self.assertTrue(
            any("response_origin" in statement for statement in count_statements),
            count_statements,
        )
        self.assertFalse(
            any("metadata_payload" in statement for statement in count_statements),
            count_statements,
        )

        public_response = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public_response.status_code, 200, public_response.get_json())
        public_results = public_response.get_json()["resultados_envivo"]
        self.assertEqual(public_results["total_respuestas"], 1)
        self.assertEqual(public_results["data_provenance"]["mode"], "real")
        self.assertEqual(
            public_results["data_provenance"]["synthetic_responses_excluded"],
            1,
        )

        live_response = self.client.get(
            f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0"
        )
        self.assertEqual(live_response.status_code, 200, live_response.get_json())
        live_results = live_response.get_json()
        self.assertEqual(live_results["total_respuestas"], 1)
        self.assertEqual(live_results["data_provenance"]["mode"], "real")
        self.assertEqual(
            live_results["data_provenance"]["synthetic_responses_excluded"],
            1,
        )

        with patch("services.encuestas_service.emit_survey_update") as emit_mock:
            emitted = emit_survey_response_update(
                db.session.get(EncEncuesta, survey_id),
                token,
            )
        self.assertTrue(emitted)
        realtime_payload = emit_mock.call_args.args[1]
        self.assertEqual(realtime_payload["total_respuestas"], 1)
        self.assertEqual(
            realtime_payload["legacy_results"]["total_respuestas"],
            1,
        )
        self.assertEqual(
            realtime_payload["legacy_results"]["data_provenance"][
                "synthetic_responses_excluded"
            ],
            1,
        )

        default_summary_response = self.client.get(
            f"/api/encuestas/{survey_id}/analytics/summary",
            headers=headers,
        )
        self.assertEqual(
            default_summary_response.status_code,
            200,
            default_summary_response.get_json(),
        )
        default_summary = default_summary_response.get_json()
        self.assertEqual(default_summary["total_respuestas"], 1)
        self.assertEqual(default_summary["data_provenance"]["mode"], "real")

        synthetic_summary_response = self.client.get(
            f"/api/encuestas/{survey_id}/analytics/summary?data_mode=synthetic",
            headers=headers,
        )
        self.assertEqual(
            synthetic_summary_response.status_code,
            200,
            synthetic_summary_response.get_json(),
        )
        synthetic_summary = synthetic_summary_response.get_json()
        self.assertEqual(synthetic_summary["total_respuestas"], 1)
        self.assertEqual(
            synthetic_summary["data_provenance"]["mode"],
            "synthetic",
        )
        self.assertEqual(
            synthetic_summary["data_provenance"]["synthetic_responses_included"],
            1,
        )

        attempted_mixed_response = self.client.get(
            f"/api/encuestas/{survey_id}/analytics/summary?data_mode=mixed",
            headers=headers,
        )
        self.assertEqual(attempted_mixed_response.status_code, 200)
        attempted_mixed = attempted_mixed_response.get_json()
        self.assertEqual(attempted_mixed["total_respuestas"], 1)
        self.assertEqual(attempted_mixed["data_provenance"]["mode"], "real")

    def test_public_realtime_room_matches_socket_with_and_without_tenant_slug(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        expected_room = f"encuesta:{self.tenant_1.slug}:{token}"

        for query in ("", f"?tenant_slug={self.tenant_1.slug}"):
            with self.subTest(query=query or "without_tenant_slug"):
                response = self.client.get(f"/api/v2/public/surveys/{token}{query}")
                self.assertEqual(response.status_code, 200, response.get_json())
                realtime = response.get_json()["realtime"]
                contract_room = realtime["room"]

                self.assertEqual(contract_room, expected_room)
                self.assertEqual(realtime["rooms"], [expected_room])
                self.assertEqual(realtime["socket"]["join_payload"], {"room": expected_room})
                self.assertTrue(_is_authorized_survey_room(contract_room))

                with patch("socket_service.socketio.emit") as socket_emit:
                    emit_survey_update(
                        token,
                        {"tenant_id": self.tenant_1.id, "total_respuestas": 1},
                    )

                emitted_rooms = {call.kwargs.get("room") for call in socket_emit.call_args_list}
                self.assertEqual(emitted_rooms, {contract_room})

        self.assertFalse(_is_authorized_survey_room(f"encuesta:{self.tenant_2.slug}:{token}"))

    def test_v2_public_live_results_hidden_when_not_enabled(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        payload = self._create_payload()
        payload["show_live_results"] = False

        survey_id = self.client.post("/api/v2/surveys", json=payload, headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_resp = self._post_public_response(
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
        voter = self._create_user("identified-voter@test.com", "usuario", self.tenant_1.slug)
        db.session.commit()
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

        missing_identity = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "user_id": voter.id, "dni": "32877851"},
        )
        self.assertEqual(missing_identity.status_code, 401, missing_identity.get_json())
        missing_payload = missing_identity.get_json()
        self.assertEqual(missing_payload.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(missing_payload.get("reason_code"), "authentication_required")
        self.assertEqual(missing_payload.get("action_hint"), "authenticate")
        self.assertEqual(missing_payload.get("required_identity"), ["bearer"])

        identified = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "dni": "32877851"},
            headers=self._auth(voter),
        )
        self.assertEqual(identified.status_code, 201, identified.get_json())

        duplicate = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "dni": "32877851"},
            headers=self._auth(voter),
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())

    def test_public_response_body_user_ids_are_ignored_for_anonymous_votes(self):
        forged_target = self._create_user("forged-target@test.com", "usuario", self.tenant_1.slug)
        db.session.commit()

        payload = self._create_payload()
        payload["uniqueness_policy"] = "libre"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)
        encuesta = db.session.get(EncEncuesta, survey_id)
        encuesta.puntos_recompensa = 35
        db.session.commit()

        submissions = (
            (f"/api/v2/public/surveys/{token}/respond", "user_id", "anon-v2-forged"),
            (f"/api/public/encuestas/{token}/responder", "userId", "anon-v1-forged"),
            (
                f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
                "user_id",
                "anon-pwa-forged",
            ),
        )
        for endpoint, identity_key, anon_id in submissions:
            with self.subTest(endpoint=endpoint, identity_key=identity_key):
                response = self._post_public_response(
                    endpoint,
                    json={**answer, identity_key: forged_target.id, "anon_id": anon_id},
                    headers={"X-Anon-Id": anon_id},
                )
                self.assertEqual(response.status_code, 201, response.get_json())

        responses = EncRespuesta.query.filter_by(encuesta_id=survey_id).order_by(EncRespuesta.id.asc()).all()
        self.assertEqual(len(responses), 3)
        self.assertEqual([response.user_id for response in responses], [None, None, None])
        db.session.refresh(forged_target)
        self.assertEqual(forged_target.saldo_puntos or 0, 0)
        self.assertEqual(
            PointsTransaction.query.filter_by(user_id=forged_target.id, tipo="encuesta").count(),
            0,
        )

    def test_por_usuario_requires_bearer_on_all_public_response_routes(self):
        forged_target = self._create_user("required-forged-target@test.com", "usuario", self.tenant_1.slug)
        db.session.commit()

        payload = self._create_payload()
        payload["uniqueness_policy"] = "por_usuario"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)

        public_payload = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        self.assertEqual(public_payload.get("auth_mode"), "required")
        self.assertEqual((public_payload.get("frontend_contract") or {}).get("auth_mode"), "required")
        self.assertEqual(
            ((public_payload.get("frontend_contract") or {}).get("identity") or {}).get("provider"),
            "chatboc_session",
        )

        endpoints = (
            f"/api/v2/public/surveys/{token}/respond",
            f"/api/public/encuestas/{token}/responder",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                response = self._post_public_response(
                    endpoint,
                    json={**answer, "user_id": forged_target.id},
                )
                self.assertEqual(response.status_code, 401, response.get_json())
                self.assertEqual(response.get_json().get("reason_code"), "authentication_required")

        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 0)

    def test_authenticated_identity_wins_and_duplicate_reward_is_unique_across_public_routes(self):
        voter = self._create_user("reward-voter@test.com", "usuario", self.tenant_1.slug)
        forged_target = self._create_user("reward-forged-target@test.com", "usuario", self.tenant_1.slug)
        db.session.commit()

        payload = self._create_payload()
        payload["uniqueness_policy"] = "por_usuario"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)
        encuesta = db.session.get(EncEncuesta, survey_id)
        encuesta.puntos_recompensa = 45
        db.session.commit()

        first = self._post_public_response(
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
            json={**answer, "userId": forged_target.id},
            headers=self._auth(voter),
        )
        self.assertEqual(first.status_code, 201, first.get_json())

        legacy_duplicate = self._post_public_response(
            f"/api/public/encuestas/{token}/responder",
            json={**answer, "userId": forged_target.id},
            headers=self._auth(voter),
        )
        self.assertEqual(legacy_duplicate.status_code, 409, legacy_duplicate.get_json())
        self.assertEqual(
            legacy_duplicate.get_json().get("reason_code"),
            "survey_response_duplicate",
        )
        self.assertNotIn("response_id", legacy_duplicate.get_json())
        self.assertNotIn("receipt", legacy_duplicate.get_json())

        duplicate = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "user_id": forged_target.id},
            headers=self._auth(voter),
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())

        responses = EncRespuesta.query.filter_by(encuesta_id=survey_id).all()
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].user_id, voter.id)
        db.session.refresh(voter)
        db.session.refresh(forged_target)
        self.assertEqual(voter.saldo_puntos, 45)
        self.assertEqual(forged_target.saldo_puntos or 0, 0)
        reward_transactions = PointsTransaction.query.filter_by(
            user_id=voter.id,
            tenant_id=self.tenant_1.id,
            tipo="encuesta",
        ).all()
        self.assertEqual(len(reward_transactions), 1)
        self.assertEqual(reward_transactions[0].delta, 45)

    def test_portal_response_uses_authenticated_identity_and_durable_receipt(self):
        voter = self._create_user(
            "portal-receipt-voter@test.com",
            "usuario",
            self.tenant_1.slug,
        )
        forged_target = self._create_user(
            "portal-receipt-forged@test.com",
            "usuario",
            self.tenant_1.slug,
        )
        db.session.commit()

        create_payload = self._create_payload()
        create_payload["uniqueness_policy"] = "por_usuario"
        admin_headers, survey_id, token, _, answer = (
            self._create_published_answer_context(create_payload)
        )
        encuesta = db.session.get(EncEncuesta, survey_id)
        encuesta.puntos_recompensa = 25
        db.session.commit()

        submission_id = "portal-survey-receipt-0001"
        response_payload = {
            **answer,
            "submission_id": submission_id,
            "user_id": forged_target.id,
        }
        portal_headers = {
            **self._auth(voter),
            "Idempotency-Key": submission_id,
        }
        endpoint = (
            f"/api/v1/portal/{self.tenant_1.slug}/surveys/{token}/responses"
        )

        first = self.client.post(
            endpoint,
            json=response_payload,
            headers=portal_headers,
        )
        closed = self.client.post(
            f"/api/v2/surveys/{survey_id}/close",
            headers=admin_headers,
        )
        replay = self.client.post(
            endpoint,
            json=response_payload,
            headers=portal_headers,
        )

        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(closed.status_code, 200, closed.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        first_ack = first.get_json()
        replay_ack = replay.get_json()
        self.assertTrue(first_ack["persisted"])
        self.assertFalse(first_ack["replayed"])
        self.assertTrue(replay_ack["persisted"])
        self.assertTrue(replay_ack["replayed"])
        self.assertEqual(first_ack["response_id"], replay_ack["response_id"])
        self.assertEqual(
            replay_ack["idempotency"]["disposition"],
            "replayed",
        )

        responses = EncRespuesta.query.filter_by(encuesta_id=survey_id).all()
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0].user_id, voter.id)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)
        db.session.refresh(voter)
        db.session.refresh(forged_target)
        self.assertEqual(voter.saldo_puntos, 25)
        self.assertEqual(forged_target.saldo_puntos or 0, 0)
        self.assertEqual(
            PointsTransaction.query.filter_by(
                user_id=voter.id,
                tenant_id=self.tenant_1.id,
                tipo="encuesta",
            ).count(),
            1,
        )

    def test_authenticated_free_responses_only_grant_one_survey_reward(self):
        voter = self._create_user("free-reward-voter@test.com", "usuario", self.tenant_1.slug)
        db.session.commit()

        payload = self._create_payload()
        payload["uniqueness_policy"] = "libre"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)
        encuesta = db.session.get(EncEncuesta, survey_id)
        encuesta.puntos_recompensa = 30
        db.session.commit()

        submissions = (
            f"/api/v2/public/surveys/{token}/respond",
            f"/api/public/encuestas/{token}/responder",
        )
        for index, endpoint in enumerate(submissions, start=1):
            response = self._post_public_response(
                endpoint,
                json={**answer, "anon_id": f"free-reward-{index}"},
                headers={**self._auth(voter), "X-Anon-Id": f"free-reward-{index}"},
            )
            self.assertEqual(response.status_code, 201, response.get_json())

        responses = EncRespuesta.query.filter_by(encuesta_id=survey_id).all()
        self.assertEqual(len(responses), 2)
        self.assertEqual({response.user_id for response in responses}, {voter.id})
        db.session.refresh(voter)
        self.assertEqual(voter.saldo_puntos, 30)
        reward_transactions = PointsTransaction.query.filter_by(
            user_id=voter.id,
            tenant_id=self.tenant_1.id,
            tipo="encuesta",
        ).all()
        self.assertEqual(len(reward_transactions), 1)
        reward_metadata = reward_transactions[0].metadata_payload or {}
        self.assertEqual(reward_metadata.get("survey_id"), survey_id)
        self.assertEqual(
            reward_metadata.get("idempotency_key"),
            f"survey_reward:{survey_id}:user:{voter.id}",
        )

    def test_v2_por_cookie_rejects_duplicate_x_anon_id(self):
        payload = self._create_payload()
        payload["uniqueness_policy"] = "por_cookie"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)
        anon_headers = {"X-Anon-Id": "stable-browser-visitor"}

        first = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "anon_id": "changing-body-id-1"},
            headers=anon_headers,
        )
        self.assertEqual(first.status_code, 201, first.get_json())

        duplicate = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "anon_id": "changing-body-id-2"},
            headers=anon_headers,
        )
        self.assertEqual(duplicate.status_code, 409, duplicate.get_json())
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 1)

    def test_v2_por_cookie_rejects_response_without_stable_fingerprint(self):
        payload = self._create_payload()
        payload["uniqueness_policy"] = "por_cookie"
        _, survey_id, token, _, answer = self._create_published_answer_context(payload)

        no_cookie_client = self.app.test_client(use_cookies=False)
        response = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json=answer,
            client=no_cookie_client,
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        error = response.get_json()
        self.assertEqual(error.get("reason_code"), "stable_fingerprint_required")
        self.assertEqual(error.get("action_hint"), "provide_anon_id")
        self.assertEqual(error.get("required_identifiers"), ["anon_id"])
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 0)

    def test_v2_identity_aliases_roundtrip_create_update_public_and_respond(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        payload = self._create_payload()
        payload.update(
            {
                "anonimato": False,
                "requiere_datos_contacto": True,
                "uniqueness_policy": "libre",
            }
        )

        create_resp = self.client.post("/api/v2/surveys", json=payload, headers=headers)
        self.assertEqual(create_resp.status_code, 201, create_resp.get_json())
        created = create_resp.get_json()
        survey_id = created["id"]
        self.assertFalse(created["anonimo_permitido"])
        self.assertFalse(created["anonimato"])
        self.assertTrue(created["requiere_identidad"])
        self.assertTrue(created["requiere_datos_contacto"])

        admin_resp = self.client.get(f"/api/v2/surveys/{survey_id}", headers=headers)
        self.assertEqual(admin_resp.status_code, 200, admin_resp.get_json())
        self.assertFalse(admin_resp.get_json()["anonimato"])
        self.assertTrue(admin_resp.get_json()["requiere_datos_contacto"])

        update_resp = self.client.patch(
            f"/api/v2/surveys/{survey_id}",
            json={"anonimato": True, "requiere_datos_contacto": False},
            headers=headers,
        )
        self.assertEqual(update_resp.status_code, 200, update_resp.get_json())
        updated = update_resp.get_json()
        self.assertTrue(updated["anonimo_permitido"])
        self.assertTrue(updated["anonimato"])
        self.assertFalse(updated["requiere_identidad"])
        self.assertFalse(updated["requiere_datos_contacto"])
        self.assertEqual(len(updated["preguntas"]), 1)

        publish_resp = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers)
        self.assertEqual(publish_resp.status_code, 200, publish_resp.get_json())
        token = publish_resp.get_json()["public_token"]
        public_resp = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public_resp.status_code, 200, public_resp.get_json())
        public_payload = public_resp.get_json()
        self.assertTrue(public_payload["anonimo_permitido"])
        self.assertTrue(public_payload["anonimato"])
        self.assertFalse(public_payload["requiere_identidad"])
        self.assertFalse(public_payload["requiere_datos_contacto"])
        self.assertEqual(public_payload.get("auth_mode"), "anonymous")

        question_id = public_payload["preguntas"][0]["id"]
        option_id = public_payload["preguntas"][0]["opciones"][0]["id"]
        respond_resp = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={"respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}]},
        )
        self.assertEqual(respond_resp.status_code, 201, respond_resp.get_json())

        public_after = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        self.assertTrue(public_after["anonimato"])
        self.assertFalse(public_after["requiere_datos_contacto"])

    def test_v2_public_response_live_action_preserves_tenant_slug(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}?tenant_slug={self.tenant_1.slug}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_resp = self._post_public_response(
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

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        first_resp = self._post_public_response(
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

        limited_resp = self._post_public_response(
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

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch("routes.v2.surveys.verify_turnstile", return_value=False) as verify_turnstile_mock:
            response = self._post_public_response(
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

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch.dict(os.environ, {"CLOUDFLARE_TURNSTILE_SECRET_KEY": "", "TURNSTILE_SECRET_KEY": ""}):
            with patch("routes.v2.surveys.verify_turnstile", return_value=True) as verify_turnstile_mock:
                response = self._post_public_response(
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

        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch("routes.v2.surveys.verify_turnstile", return_value=True) as verify_turnstile_mock:
            response = self._post_public_response(
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

    def test_v2_demo_public_survey_includes_turnstile_contract_when_enforced(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        token = "demo-gobierno-junin-prioridades-barriales"

        response = self.client.get(f"/api/v2/public/surveys/{token}")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "surveys.public.v2")
        self.assertEqual(payload.get("legacy_contract_version"), "encuestas.public.v1")
        self.assertTrue(payload.get("demo_mode"))
        self.assertEqual(payload.get("security", {}).get("status"), "required")
        self.assertTrue(payload.get("frontend_contract", {}).get("turnstile", {}).get("required"))
        self.assertEqual(
            payload.get("links", {}).get("respond_endpoint"),
            f"/api/v2/public/surveys/{token}/respond",
        )

    def test_v2_demo_public_response_uses_turnstile_before_ack(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        token = "demo-gobierno-junin-prioridades-barriales"
        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")

        with patch("routes.v2.surveys.verify_turnstile", return_value=True) as verify_turnstile_mock:
            response = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json={
                    "anon_id": "anon-demo-v2",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                headers={"X-Turnstile-Token": "valid-turnstile-token"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "surveys.public_response.v2")
        self.assertEqual(payload.get("legacy_contract_version"), "demo.survey_response_ack.v1")
        self.assertTrue(payload.get("demo_mode"))
        self.assertFalse(payload.get("persisted"))
        self.assertFalse(payload.get("durable"))
        self.assertEqual(payload.get("persistence", {}).get("state"), "not_persisted")
        self.assertFalse(payload.get("persistence", {}).get("database_write"))
        self.assertFalse(payload.get("persistence", {}).get("live_results_mutated"))
        self.assertEqual(payload.get("seeded_responses_after"), 100)
        self.assertEqual(payload.get("simulated_view_responses_after"), 101)
        self.assertEqual(
            payload.get("frontend_contract", {}).get("persistence"),
            payload.get("persistence"),
        )
        self.assertEqual(payload.get("security", {}).get("status"), "verified")
        self.assertEqual(
            payload.get("links", {}).get("live_results_endpoint"),
            f"/api/v2/public/surveys/{token}/live-results",
        )
        self.assertEqual(EncRespuesta.query.count(), 0)
        verify_turnstile_mock.assert_called_once()

    def test_v2_demo_live_results_use_v2_links(self):
        token = "demo-gobierno-junin-prioridades-barriales"

        response = self.client.get(f"/api/v2/public/surveys/{token}/live-results")

        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload.get("contract_version"), "surveys.live_results.v2")
        self.assertTrue(payload.get("demo_mode"))
        provenance = payload.get("data_provenance") or {}
        self.assertEqual(provenance.get("mode"), "synthetic")
        self.assertEqual(provenance.get("synthetic_responses_included"), 100)
        self.assertEqual(payload.get("response_provenance"), provenance)
        self.assertTrue(
            payload.get("heatmap", {})
            .get("metadata", {})
            .get("using_synthetic_points")
        )
        question = next(iter((payload.get("preguntas") or {}).values()))
        options = question.get("opciones") or []
        self.assertEqual(sum(option.get("votos", 0) for option in options), 100)
        self.assertEqual(sum(option.get("porcentaje", 0) for option in options), 100)
        self.assertEqual(
            payload.get("links", {}).get("live_results_endpoint"),
            f"/api/v2/public/surveys/{token}/live-results",
        )
        self.assertEqual(
            payload.get("realtime", {}).get("polling", {}).get("href"),
            f"/api/v2/public/surveys/{token}/live-results",
        )

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
                "schema_version": "survey-builder.v2",
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
        self.assertEqual(payload.get("schema_version"), "survey-builder.v2")
        self.assertTrue(payload.get("persisted"))
        self.assertEqual(payload.get("revision"), 1)
        self.assertIsNotNone(payload.get("created_at"))
        self.assertIsNotNone(payload.get("updated_at"))
        self.assertEqual(SurveyDraft.query.count(), 1)
        stored = SurveyDraft.query.one()
        self.assertEqual(stored.schema_version, "survey-builder.v2")
        self.assertEqual(SurveyDraftIdempotency.query.count(), 1)

        restored = self.client.get(
            f"/api/v2/surveys/draft/{payload['draft_id']}",
            headers=headers,
        )
        self.assertEqual(restored.status_code, 200, restored.get_json())
        restored_payload = restored.get_json()
        self.assertEqual(restored_payload.get("revision"), 1)
        self.assertEqual(restored_payload.get("draft", {}).get("title"), "Borrador offline")
        self.assertEqual(
            restored_payload.get("draft", {}).get("questions", [])[0].get("title"),
            "",
        )

    def test_survey_draft_replay_is_idempotent_and_changed_content_increments_revision(self):
        headers = {
            **self._auth(self.admin_1),
            "X-Tenant-Slug": self.tenant_1.slug,
            "Idempotency-Key": "durable-draft-1",
        }
        initial_payload = {
            "draft_id": "draft-durable-1",
            "title": "Entrevista vecinal",
            "questions": [],
        }

        first = self.client.post("/api/v2/surveys/draft", json=initial_payload, headers=headers)
        replay = self.client.post("/api/v2/surveys/draft", json=initial_payload, headers=headers)

        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(first.get_json().get("revision"), 1)
        self.assertEqual(replay.get_json().get("revision"), 1)
        self.assertEqual(SurveyDraft.query.filter_by(tenant_id=self.tenant_1.id).count(), 1)

        reused_for_another_draft = self.client.post(
            "/api/v2/surveys/draft",
            json={**initial_payload, "draft_id": "draft-durable-duplicate"},
            headers=headers,
        )
        self.assertEqual(reused_for_another_draft.status_code, 409, reused_for_another_draft.get_json())
        self.assertEqual(
            reused_for_another_draft.get_json().get("reason_code"),
            "draft_idempotency_conflict",
        )
        self.assertEqual(SurveyDraft.query.filter_by(tenant_id=self.tenant_1.id).count(), 1)

        same_key_changed_content = self.client.post(
            "/api/v2/surveys/draft",
            json={**initial_payload, "revision": 1, "description": "No debe aplicarse"},
            headers=headers,
        )
        self.assertEqual(same_key_changed_content.status_code, 409, same_key_changed_content.get_json())
        self.assertEqual(
            same_key_changed_content.get_json().get("reason_code"),
            "draft_idempotency_conflict",
        )
        unchanged = SurveyDraft.query.filter_by(
            tenant_id=self.tenant_1.id,
            draft_id=initial_payload["draft_id"],
        ).one()
        self.assertEqual(unchanged.revision, 1)
        self.assertNotIn("description", unchanged.payload)

        missing_revision = self.client.post(
            "/api/v2/surveys/draft",
            json={**initial_payload, "description": "Tampoco debe aplicarse"},
            headers={**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug},
        )
        self.assertEqual(missing_revision.status_code, 409, missing_revision.get_json())
        self.assertEqual(missing_revision.get_json().get("reason_code"), "draft_revision_conflict")
        self.assertEqual(missing_revision.get_json().get("current_revision"), 1)
        unchanged = SurveyDraft.query.filter_by(
            tenant_id=self.tenant_1.id,
            draft_id=initial_payload["draft_id"],
        ).one()
        self.assertEqual(unchanged.revision, 1)
        self.assertNotIn("description", unchanged.payload)

        changed_headers = {**headers, "Idempotency-Key": "durable-draft-2"}
        changed = self.client.post(
            "/api/v2/surveys/draft",
            json={**initial_payload, "revision": 1, "description": "Nueva descripcion"},
            headers=changed_headers,
        )
        self.assertEqual(changed.status_code, 200, changed.get_json())
        self.assertEqual(changed.get_json().get("revision"), 2)
        self.assertEqual(changed.get_json().get("draft", {}).get("description"), "Nueva descripcion")
        self.assertEqual(SurveyDraftIdempotency.query.count(), 2)

        delayed_replay = self.client.post(
            "/api/v2/surveys/draft",
            json=initial_payload,
            headers=headers,
        )
        self.assertEqual(delayed_replay.status_code, 200, delayed_replay.get_json())
        self.assertEqual(delayed_replay.get_json().get("revision"), 2)
        self.assertEqual(
            delayed_replay.get_json().get("draft", {}).get("description"),
            "Nueva descripcion",
        )

    def test_survey_draft_rejects_payload_above_configured_limit(self):
        self.app.config["SURVEY_DRAFT_MAX_BYTES"] = 256
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        response = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": "draft-too-large", "description": "x" * 512},
            headers=headers,
        )

        self.assertEqual(response.status_code, 413, response.get_json())
        self.assertEqual(response.get_json().get("reason_code"), "survey_draft_too_large")
        self.assertEqual(response.get_json().get("action_hint"), "reduce_draft_size")
        self.assertEqual(SurveyDraft.query.count(), 0)

    def test_survey_draft_stale_revision_returns_conflict_without_overwrite(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        draft_id = "draft-revision-conflict"
        first = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": draft_id, "title": "Version 1", "questions": []},
            headers=headers,
        )
        self.assertEqual(first.status_code, 200, first.get_json())
        second = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": draft_id, "revision": 1, "title": "Version 2", "questions": []},
            headers=headers,
        )
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(second.get_json().get("revision"), 2)

        stale = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": draft_id, "revision": 1, "title": "Version obsoleta", "questions": []},
            headers=headers,
        )
        self.assertEqual(stale.status_code, 409, stale.get_json())
        conflict = stale.get_json()
        self.assertEqual(conflict.get("reason_code"), "draft_revision_conflict")
        self.assertEqual(conflict.get("current_revision"), 2)
        self.assertEqual(conflict.get("draft", {}).get("title"), "Version 2")

        current = SurveyDraft.query.filter_by(tenant_id=self.tenant_1.id, draft_id=draft_id).one()
        self.assertEqual(current.revision, 2)
        self.assertEqual(current.payload.get("title"), "Version 2")

    def test_survey_draft_restore_and_listing_are_tenant_scoped(self):
        shared_draft_id = "draft-shared-id"
        headers_1 = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        headers_2 = {**self._auth(self.admin_2), "X-Tenant-Slug": self.tenant_2.slug}

        saved_1 = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": shared_draft_id, "title": "Tenant A", "questions": []},
            headers=headers_1,
        )
        self.assertEqual(saved_1.status_code, 200, saved_1.get_json())

        hidden_from_tenant_2 = self.client.get(
            f"/api/v2/surveys/draft/{shared_draft_id}",
            headers=headers_2,
        )
        self.assertEqual(hidden_from_tenant_2.status_code, 404, hidden_from_tenant_2.get_json())

        saved_2 = self.client.post(
            "/api/v2/surveys/draft",
            json={"draft_id": shared_draft_id, "title": "Tenant B", "questions": []},
            headers=headers_2,
        )
        self.assertEqual(saved_2.status_code, 200, saved_2.get_json())
        self.assertEqual(SurveyDraft.query.filter_by(draft_id=shared_draft_id).count(), 2)

        restored_1 = self.client.get(f"/api/v2/surveys/draft/{shared_draft_id}", headers=headers_1)
        restored_2 = self.client.get(f"/api/v2/surveys/draft/{shared_draft_id}", headers=headers_2)
        self.assertEqual(restored_1.get_json().get("draft", {}).get("title"), "Tenant A")
        self.assertEqual(restored_2.get_json().get("draft", {}).get("title"), "Tenant B")

        listed_1 = self.client.get("/api/v2/surveys/drafts?limit=1", headers=headers_1)
        self.assertEqual(listed_1.status_code, 200, listed_1.get_json())
        self.assertEqual(listed_1.get_json().get("total"), 1)
        self.assertEqual(listed_1.get_json().get("items", [])[0].get("draft", {}).get("title"), "Tenant A")

    def test_free_plan_can_create_and_save_survey_draft_as_self_service_module(self):
        self.tenant_1.plan = "free"
        db.session.add(self.tenant_1)
        db.session.commit()
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        create_resp = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers)
        self.assertEqual(create_resp.status_code, 201, create_resp.get_json())
        create_payload = create_resp.get_json()
        self.assertEqual(create_payload["tenant_id"], self.tenant_1.id)

        draft_resp = self.client.post(
            "/api/v2/surveys/draft",
            json={"title": "Borrador self service", "questions": []},
            headers=headers,
        )
        self.assertEqual(draft_resp.status_code, 200, draft_resp.get_json())
        self.assertTrue(draft_resp.get_json()["ok"])

    def test_closed_survey_rejects_public_responses(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        created = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()
        survey_id = created["id"]
        publish = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()
        token = publish["public_token"]

        close_resp = self.client.post(f"/api/v2/surveys/{survey_id}/close", headers=headers)
        self.assertEqual(close_resp.status_code, 200)

        failed_resp = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={"respuestas": []},
        )
        self.assertEqual(failed_resp.status_code, 403)


    def test_analytics_surveys_returns_votes_for_tenant(self):
        headers = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}

        survey_id = self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers).get_json()["id"]
        token = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=headers).get_json()["public_token"]

        public_get = self.client.get(f"/api/v2/public/surveys/{token}").get_json()
        question_id = public_get.get("preguntas", [])[0].get("id")
        option_id = public_get.get("preguntas", [])[0].get("opciones", [])[0].get("id")
        self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            json={"anon_id": "a-analytics-1", "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}]},
        )

        analytics_resp = self.client.get("/api/v2/analytics/surveys", headers=headers)
        self.assertEqual(analytics_resp.status_code, 200)
        stats = (analytics_resp.get_json() or {}).get("stats") or {}
        self.assertGreaterEqual(int(stats.get("total_votes") or 0), 1)

    def test_public_response_receipt_replays_once_without_side_effects(self):
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-submit-once-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-receipt-once",
            "metadata": {"submittedAt": "2026-07-28T13:00:00.000Z"},
        }
        headers = {"Idempotency-Key": submission_id}

        with patch(
            "services.encuestas_service.emit_survey_response_update",
            return_value=True,
        ) as realtime_mock:
            first = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json=payload,
                headers=headers,
            )
            replay = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json=payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        first_ack = first.get_json()
        replay_ack = replay.get_json()
        self.assertTrue(first_ack["persisted"])
        self.assertFalse(first_ack["replayed"])
        self.assertEqual(first_ack["idempotency"]["disposition"], "accepted")
        self.assertTrue(replay_ack["persisted"])
        self.assertTrue(replay_ack["replayed"])
        self.assertEqual(replay_ack["idempotency"]["disposition"], "replayed")
        self.assertEqual(first_ack["response_id"], replay_ack["response_id"])
        self.assertEqual(
            first_ack["idempotency"]["receipt_id"],
            replay_ack["idempotency"]["receipt_id"],
        )
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)
        effects = SurveyResponseEffect.query.order_by(SurveyResponseEffect.effect_type).all()
        self.assertEqual(len(effects), 2)
        self.assertEqual({effect.status for effect in effects}, {"succeeded"})
        self.assertEqual(
            AnalyticsEventV2.query.filter_by(
                tenant_id=self.tenant_1.id,
                event_name="vote_submitted",
            ).count(),
            1,
        )
        realtime_mock.assert_called_once()

    def test_public_response_receipt_conflicts_on_changed_payload(self):
        _, _, token, public_payload, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-submit-conflict-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-receipt-conflict",
        }
        headers = {"Idempotency-Key": submission_id}
        first = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=payload,
            headers=headers,
        )
        self.assertEqual(first.status_code, 201, first.get_json())

        changed = {
            **payload,
            "respuestas": [
                {
                    "pregunta_id": public_payload["preguntas"][0]["id"],
                    "opcion_id": public_payload["preguntas"][0]["opciones"][1]["id"],
                }
            ],
        }
        conflict = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=changed,
            headers=headers,
        )

        self.assertEqual(conflict.status_code, 409, conflict.get_json())
        self.assertEqual(conflict.get_json()["reason_code"], "survey_submission_id_conflict")
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_response_receipt_replay_skips_rate_limit_and_turnstile(self):
        self.app.config["PUBLIC_ENCUESTAS_RATE_LIMIT"] = 1
        self.app.config["PUBLIC_ENCUESTAS_RATE_PERIOD"] = 60
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-submit-turnstile-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-receipt-turnstile",
            "turnstile_token": "one-shot-token",
        }
        headers = {"Idempotency-Key": submission_id}

        with patch("routes.v2.surveys.verify_turnstile", return_value=True) as first_verify:
            first = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json=payload,
                headers=headers,
            )
        with patch("routes.v2.surveys.verify_turnstile", return_value=False) as replay_verify:
            replay = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json=payload,
                headers=headers,
            )

        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["replayed"])
        self.assertEqual(replay.get_json()["security"]["status"], "receipt_replay")
        first_verify.assert_called_once()
        replay_verify.assert_not_called()
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_response_ack_enrichment_failure_still_returns_durable_ack(self):
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-submit-ack-fallback-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-ack-fallback",
        }
        headers = {"Idempotency-Key": submission_id}

        with patch(
            "routes.v2.surveys._build_public_survey_response_ack",
            side_effect=RuntimeError("simulated enrichment failure"),
        ):
            response = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json=payload,
                headers=headers,
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        ack = response.get_json()
        self.assertTrue(ack["persisted"])
        self.assertFalse(ack["replayed"])
        self.assertEqual(
            ack["warning"]["reason_code"],
            "survey_response_ack_enrichment_failed",
        )
        self.assertEqual(ack["idempotency"]["disposition"], "accepted")
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_response_receipt_is_shared_by_v2_legacy_and_pwa(self):
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-submit-cross-route-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-receipt-cross-route",
            "source": "web",
        }
        headers = {"Idempotency-Key": submission_id}

        first = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=payload,
            headers=headers,
        )
        legacy = self.client.post(
            f"/api/public/encuestas/v1/{token}/responder",
            json=payload,
            headers=headers,
        )
        pwa = self.client.post(
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
            json=payload,
            headers=headers,
        )

        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(legacy.status_code, 200, legacy.get_json())
        self.assertEqual(pwa.status_code, 200, pwa.get_json())
        response_ids = {
            first.get_json()["response_id"],
            legacy.get_json()["response_id"],
            pwa.get_json()["response_id"],
        }
        self.assertEqual(len(response_ids), 1)
        self.assertTrue(legacy.get_json()["idempotency"]["replayed"])
        self.assertTrue(pwa.get_json()["idempotency"]["replayed"])
        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_every_public_survey_alias_rejects_missing_turnstile_before_write(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())

        expected_error = None
        for index, path in enumerate(self._public_response_alias_paths(token), start=1):
            submission_id = f"survey-alias-turnstile-{index:02d}"
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    json={
                        **answer,
                        "submission_id": submission_id,
                        "anon_id": f"anon-alias-turnstile-{index:02d}",
                    },
                    headers={
                        "Idempotency-Key": submission_id,
                        "X-Forwarded-For": "198.51.100.151",
                    },
                )
                self.assertEqual(response.status_code, 400, response.get_json())
                payload = response.get_json()
                comparable = {
                    key: payload.get(key)
                    for key in (
                        "contract_version",
                        "status_code",
                        "reason_code",
                        "retryable",
                        "action_hint",
                    )
                }
                comparable["security_status"] = payload.get("security", {}).get("status")
                comparable["turnstile_required"] = payload.get("frontend_contract", {}).get(
                    "turnstile", {}
                ).get("required")
                if expected_error is None:
                    expected_error = comparable
                self.assertEqual(comparable, expected_error)
                self.assertEqual(EncRespuesta.query.count(), 0)
                self.assertEqual(SurveyResponseReceipt.query.count(), 0)

        self.assertEqual(
            expected_error,
            {
                "contract_version": "surveys.public_response.v2",
                "status_code": 400,
                "reason_code": "turnstile_verificacion_fallida",
                "retryable": True,
                "action_hint": "retry_security_challenge",
                "security_status": "verification_failed",
                "turnstile_required": True,
            },
        )

    def test_public_survey_aliases_share_one_tenant_scoped_rate_limit(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
        self.app.config["PUBLIC_ENCUESTAS_RATE_LIMIT"] = 1
        self.app.config["PUBLIC_ENCUESTAS_RATE_PERIOD"] = 60
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        common_headers = {
            "CF-Connecting-IP": "203.0.113.152",
            "X-Forwarded-For": "198.51.100.152",
        }

        first_id = "survey-shared-limit-legacy-01"
        first = self.client.post(
            f"/api/public/encuestas/v1/{token}/responder?tenant_slug={self.tenant_1.slug}",
            json={**answer, "submission_id": first_id, "anon_id": "anon-shared-limit-1"},
            headers={**common_headers, "Idempotency-Key": first_id},
        )
        self.assertEqual(first.status_code, 201, first.get_json())

        blocked_paths = (
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
            f"/api/v2/public/surveys/{token}/respond",
        )
        for index, path in enumerate(blocked_paths, start=2):
            submission_id = f"survey-shared-limit-{index:02d}"
            with self.subTest(path=path):
                blocked = self.client.post(
                    path,
                    json={
                        **answer,
                        "submission_id": submission_id,
                        "anon_id": f"anon-shared-limit-{index}",
                    },
                    headers={
                        "CF-Connecting-IP": f"203.0.113.{160 + index}",
                        "X-Forwarded-For": f"198.51.100.{160 + index}",
                        "Idempotency-Key": submission_id,
                    },
                )
                self.assertEqual(blocked.status_code, 429, blocked.get_json())
                self.assertEqual(blocked.get_json().get("reason_code"), "rate_limited")
                self.assertEqual(
                    blocked.get_json().get("contract_version"),
                    "surveys.public_response.v2",
                )
                self.assertEqual(blocked.headers.get("X-RateLimit-Remaining"), "0")

        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_survey_alias_security_remains_optional_when_explicitly_disabled(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        paths = (
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_1.slug}",
            f"/api/public/encuestas/v1/{token}/responder?tenant_slug={self.tenant_1.slug}",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
        )

        for index, path in enumerate(paths, start=1):
            submission_id = f"survey-security-disabled-{index:02d}"
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    json={
                        **answer,
                        "submission_id": submission_id,
                        "anon_id": f"anon-security-disabled-{index}",
                    },
                    headers={"Idempotency-Key": submission_id},
                )
                self.assertEqual(response.status_code, 201, response.get_json())

        self.assertEqual(EncRespuesta.query.count(), 3)
        self.assertEqual(SurveyResponseReceipt.query.count(), 3)

    def test_legacy_and_pwa_accept_verified_turnstile_before_persisting(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "true"
        self.app.config["CLOUDFLARE_TURNSTILE_SECRET_KEY"] = "test-turnstile-secret"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        paths = (
            f"/api/public/encuestas/v1/{token}/responder?tenant_slug={self.tenant_1.slug}",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
        )

        with patch(
            "services.public_survey_intake.verify_turnstile",
            return_value=True,
        ) as verify_turnstile_mock:
            for index, path in enumerate(paths, start=1):
                submission_id = f"survey-alias-turnstile-valid-{index:02d}"
                with self.subTest(path=path):
                    response = self.client.post(
                        path,
                        json={
                            **answer,
                            "submission_id": submission_id,
                            "anon_id": f"anon-alias-turnstile-valid-{index}",
                        },
                        headers={
                            "Idempotency-Key": submission_id,
                            "X-Turnstile-Token": f"valid-turnstile-{index}",
                        },
                    )
                    self.assertEqual(response.status_code, 201, response.get_json())

        self.assertEqual(verify_turnstile_mock.call_count, 2)
        self.assertEqual(EncRespuesta.query.count(), 2)
        self.assertEqual(SurveyResponseReceipt.query.count(), 2)

    def test_public_survey_aliases_reject_wrong_tenant_without_write(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        wrong_tenant_paths = (
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_2.slug}",
            f"/api/public/encuestas/v1/{token}/responder?tenant_slug={self.tenant_2.slug}",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_2.slug}",
        )

        for index, path in enumerate(wrong_tenant_paths, start=1):
            submission_id = f"survey-wrong-tenant-{index:02d}"
            with self.subTest(path=path):
                response = self.client.post(
                    path,
                    json={
                        **answer,
                        "submission_id": submission_id,
                        "anon_id": f"anon-wrong-tenant-{index}",
                    },
                    headers={"Idempotency-Key": submission_id},
                )
                self.assertEqual(response.status_code, 404, response.get_json())

        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(SurveyResponseReceipt.query.count(), 0)

    def test_public_survey_reads_fail_closed_for_explicit_wrong_tenant(self):
        _, _, token, _, _ = self._create_published_answer_context(self._create_payload())

        wrong_tenant_paths = (
            f"/api/v2/public/surveys/{token}?tenant_slug={self.tenant_2.slug}",
            f"/api/v2/public/surveys/{token}/live-results?tenant_slug={self.tenant_2.slug}&include_heatmap=0",
            f"/api/public/encuestas/v1/{token}?tenant_slug={self.tenant_2.slug}",
            f"/api/public/encuestas/v1/{token}/live-results?tenant_slug={self.tenant_2.slug}&include_heatmap=0",
            f"/api/pwa/public/surveys/{token}?tenant={self.tenant_2.slug}",
        )
        for path in wrong_tenant_paths:
            with self.subTest(path=path):
                denied = self.client.get(path)
                self.assertEqual(denied.status_code, 404, denied.get_json())
                self.assertEqual(denied.get_json().get("reason_code"), "survey_not_found")
                self.assertNotIn("preguntas", denied.get_json())
                self.assertNotIn("total_respuestas", denied.get_json())

        for query in ("", f"?tenant_slug={self.tenant_1.slug}"):
            with self.subTest(query=query or "without_tenant"):
                public_response = self.client.get(
                    f"/api/v2/public/surveys/{token}{query}"
                )
                self.assertEqual(
                    public_response.status_code,
                    200,
                    public_response.get_json(),
                )

                separator = "&" if query else "?"
                live_response = self.client.get(
                    f"/api/v2/public/surveys/{token}/live-results"
                    f"{query}{separator}include_heatmap=0"
                )
                self.assertEqual(live_response.status_code, 200, live_response.get_json())

        unknown_tenant = self.client.get(
            f"/api/v2/public/surveys/{token}?tenant_slug=tenant-that-does-not-exist",
            # A valid bearer tenant must not replace an explicitly invalid
            # tenant selector on a public read.
            headers=self._auth(self.admin_1),
        )
        self.assertEqual(unknown_tenant.status_code, 404, unknown_tenant.get_json())

        for path in (
            f"/api/v2/public/surveys/{token}?tenant_slug=%20%20%20",
            f"/api/public/encuestas/v1/{token}?tenant_slug=%20%20%20",
        ):
            with self.subTest(empty_selector_path=path):
                empty_selector = self.client.get(path, headers=self._auth(self.admin_1))
                self.assertEqual(
                    empty_selector.status_code,
                    404,
                    empty_selector.get_json(),
                )
                self.assertNotIn("preguntas", empty_selector.get_json())

        conflicting_tenant = self.client.get(
            f"/api/v2/public/surveys/{token}?tenant_slug={self.tenant_2.slug}",
            headers={"X-Tenant-Slug": self.tenant_1.slug},
        )
        self.assertEqual(
            conflicting_tenant.status_code,
            400,
            conflicting_tenant.get_json(),
        )
        self.assertEqual(
            conflicting_tenant.get_json().get("reason_code"),
            "tenant_selector_mismatch",
        )

    def test_portal_vote_cannot_use_another_tenants_public_token(self):
        _, _, token, _, answer = self._create_published_answer_context(
            self._create_payload()
        )
        submission_id = "portal-cross-tenant-survey-0001"

        denied = self.client.post(
            f"/api/v1/portal/{self.tenant_2.slug}/surveys/{token}/responses",
            json={
                **answer,
                "anon_id": "portal-cross-tenant-voter",
                "submission_id": submission_id,
            },
            headers={
                **self._auth(self.admin_2),
                "Idempotency-Key": submission_id,
                "X-Anon-Id": "portal-cross-tenant-voter",
            },
        )

        self.assertEqual(denied.status_code, 404, denied.get_json())
        self.assertEqual(denied.get_json().get("reason_code"), "survey_not_found")
        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(SurveyResponseReceipt.query.count(), 0)

    def test_public_survey_resolver_rejects_invalid_or_unmatched_tenant_id(self):
        _, _, token, _, _ = self._create_published_answer_context(self._create_payload())

        self.assertEqual(get_public_encuesta(token).tenant_id, self.tenant_1.id)
        self.assertEqual(
            get_public_encuesta(
                token,
                preferred_tenant_id=self.tenant_1.id,
                require_tenant_match=True,
            ).tenant_id,
            self.tenant_1.id,
        )
        short_token = token.rsplit("-", 1)[-1]
        self.assertEqual(get_public_encuesta(short_token).tenant_id, self.tenant_1.id)
        self.assertEqual(
            get_public_encuesta(
                short_token,
                preferred_tenant_id=self.tenant_1.id,
                require_tenant_match=True,
            ).tenant_id,
            self.tenant_1.id,
        )

        for invalid_tenant_id in (
            None,
            True,
            False,
            0,
            -1,
            " ",
            self.tenant_2.id,
            max(self.tenant_1.id, self.tenant_2.id) + 10_000,
            "not-a-tenant-id",
        ):
            with self.subTest(preferred_tenant_id=invalid_tenant_id):
                with self.assertRaises(EncuestaError) as raised:
                    get_public_encuesta(
                        token,
                        preferred_tenant_id=invalid_tenant_id,
                        require_tenant_match=True,
                    )
                self.assertEqual(raised.exception.status_code, 404)
                self.assertEqual(
                    raised.exception.payload.get("reason_code"),
                    "survey_not_found",
                )

        with self.assertRaises(EncuestaError) as short_token_denied:
            get_public_encuesta(
                short_token,
                preferred_tenant_id=self.tenant_2.id,
                require_tenant_match=True,
            )
        self.assertEqual(short_token_denied.exception.status_code, 404)
        self.assertEqual(
            short_token_denied.exception.payload.get("reason_code"),
            "survey_not_found",
        )

    def test_public_survey_replay_cannot_cross_tenant_or_disclose_receipt(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-replay-tenant-scope-01"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-replay-tenant-scope",
        }
        headers = {"Idempotency-Key": submission_id}
        accepted = self.client.post(
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_1.slug}",
            json=payload,
            headers=headers,
        )
        self.assertEqual(accepted.status_code, 201, accepted.get_json())

        wrong_tenant_paths = (
            f"/api/v2/public/surveys/{token}/respond?tenant_slug={self.tenant_2.slug}",
            f"/api/public/encuestas/v1/{token}/responder?tenant_slug={self.tenant_2.slug}",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_2.slug}",
        )
        for path in wrong_tenant_paths:
            with self.subTest(path=path):
                denied = self.client.post(path, json=payload, headers=headers)
                self.assertEqual(denied.status_code, 404, denied.get_json())
                self.assertEqual(denied.get_json().get("reason_code"), "survey_not_found")
                self.assertNotIn("response_id", denied.get_json())
                self.assertNotIn("idempotency", denied.get_json())

        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_survey_rate_limiter_failure_fails_closed_without_write(self):
        self.app.config["CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE"] = "false"
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        submission_id = "survey-rate-storage-failure-01"

        with patch(
            "services.public_survey_intake.limiter.limiter.hit",
            side_effect=RuntimeError("rate storage unavailable"),
        ):
            response = self.client.post(
                f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
                json={**answer, "submission_id": submission_id},
                headers={"Idempotency-Key": submission_id},
            )

        self.assertEqual(response.status_code, 503, response.get_json())
        self.assertEqual(
            response.get_json().get("reason_code"),
            "survey_rate_limit_unavailable",
        )
        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(SurveyResponseReceipt.query.count(), 0)

    def test_public_survey_alias_preflights_allow_turnstile_and_idempotency_headers(self):
        paths = (
            "/api/v2/public/surveys/example/respond",
            "/api/public/encuestas/v1/example/responder",
            "/public/encuestas/v1/example/respuestas",
            f"/api/pwa/public/surveys/example/respond?tenant={self.tenant_1.slug}",
            f"/api/v1/portal/{self.tenant_1.slug}/surveys/example/responses",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.options(
                    path,
                    headers={
                        "Origin": "http://localhost:8080",
                        "Access-Control-Request-Method": "POST",
                        "Access-Control-Request-Headers": (
                            "X-Turnstile-Token, X-Survey-Eligibility-Credential, "
                            "Idempotency-Key, Content-Type"
                        ),
                    },
                )
                self.assertIn(response.status_code, {200, 204}, response.get_data(as_text=True))
                allowed = response.headers.get("Access-Control-Allow-Headers", "").lower()
                self.assertIn("x-turnstile-token", allowed)
                self.assertIn("x-survey-eligibility-credential", allowed)
                self.assertIn("idempotency-key", allowed)

    def test_committed_receipt_replays_after_close_without_reopening_intake(self):
        headers, survey_id, token, public_payload, answer = (
            self._create_published_answer_context(self._create_payload())
        )
        submission_id = "survey-submit-after-close-0001"
        payload = {
            **answer,
            "submission_id": submission_id,
            "anon_id": "anon-receipt-after-close",
        }
        idempotency_headers = {"Idempotency-Key": submission_id}

        first = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=payload,
            headers=idempotency_headers,
        )
        self.assertEqual(first.status_code, 201, first.get_json())
        response_id = first.get_json()["response_id"]

        closed = self.client.post(
            f"/api/v2/surveys/{survey_id}/close",
            headers=headers,
        )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        closed_public = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(closed_public.status_code, 200, closed_public.get_json())

        changed_payload = {
            **payload,
            "respuestas": [
                {
                    "pregunta_id": public_payload["preguntas"][0]["id"],
                    "opcion_id": public_payload["preguntas"][0]["opciones"][1]["id"],
                }
            ],
        }
        changed = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=changed_payload,
            headers=idempotency_headers,
        )
        self.assertEqual(changed.status_code, 409, changed.get_json())
        self.assertEqual(
            changed.get_json()["reason_code"],
            "survey_submission_id_conflict",
        )

        fresh_payload = {
            **answer,
            "submission_id": "survey-submit-after-close-fresh-0002",
            "anon_id": "anon-receipt-after-close-fresh",
        }
        fresh = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json=fresh_payload,
            headers={"Idempotency-Key": fresh_payload["submission_id"]},
        )
        self.assertEqual(fresh.status_code, 403, fresh.get_json())

        replay_paths = (
            f"/api/v2/public/surveys/{token}/respond",
            f"/api/public/encuestas/v1/{token}/responder",
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant_1.slug}",
        )
        for path in replay_paths:
            with self.subTest(path=path):
                replay = self.client.post(
                    path,
                    json=payload,
                    headers=idempotency_headers,
                )
                self.assertEqual(replay.status_code, 200, replay.get_json())
                ack = replay.get_json()
                self.assertTrue(ack["persisted"])
                self.assertTrue(ack["replayed"])
                self.assertEqual(ack["response_id"], response_id)
                self.assertEqual(
                    ack["idempotency"]["disposition"],
                    "replayed",
                )

        self.assertEqual(EncRespuesta.query.count(), 1)
        self.assertEqual(SurveyResponseReceipt.query.count(), 1)

    def test_public_response_rejects_mismatched_submission_ids(self):
        _, _, token, _, answer = self._create_published_answer_context(self._create_payload())
        response = self.client.post(
            f"/api/v2/public/surveys/{token}/respond",
            json={**answer, "submission_id": "survey-body-key-0001"},
            headers={"Idempotency-Key": "survey-header-key-0001"},
        )

        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.get_json()["reason_code"], "survey_submission_id_mismatch")
        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(SurveyResponseReceipt.query.count(), 0)

    def test_legacy_public_response_cors_allows_idempotency_key(self):
        response = self.client.options(
            "/api/public/encuestas/v1/example/responder",
            headers={"Origin": "http://localhost:8080"},
        )

        self.assertEqual(response.status_code, 204)
        self.assertIn(
            "Idempotency-Key",
            response.headers.get("Access-Control-Allow-Headers", ""),
        )

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
        self.assertFalse(ack["persisted"])
        self.assertFalse(ack["durable"])
        self.assertEqual(ack["persistence"]["state"], "not_persisted")
        self.assertFalse(ack["persistence"]["database_write"])
        self.assertFalse(ack["persistence"]["live_results_mutated"])
        self.assertEqual(ack["seeded_responses_after"], 100)
        self.assertEqual(ack["simulated_view_responses_after"], 101)
        self.assertEqual(ack["links"]["qr_endpoint"], item["links"]["qr_endpoint"])
        self.assertEqual(ack["realtime"]["transports"], ["polling"])


if __name__ == "__main__":
    unittest.main()
