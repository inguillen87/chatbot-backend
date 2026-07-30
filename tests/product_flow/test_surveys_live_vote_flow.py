import os
import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import AnalyticsEventV2, EncRespuesta, TenantProfile, User


class ProductFlowSurveyConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class ProductFlowSurveyLiveVoteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ProductFlowSurveyConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Flow Surveys",
            email="flow-surveys@test.com",
            rol="admin",
            tenant_slug="flow-surveys",
        )
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="flow-surveys",
            nombre="Flow Surveys",
            tipo="municipio",
            pyme_id=self.admin.id,
            plan="full",
        )
        db.session.add(self.tenant)
        db.session.commit()
        self.admin.tenant_id = self.tenant.id
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _auth(self):
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.utcnow() + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": self.tenant.slug}

    def _create_live_vote(self):
        now = datetime.utcnow()
        payload = {
            "title": "Votacion product flow",
            "description": "Demo realtime",
            "channel": "web",
            "live_vote": True,
            "show_live_results": True,
            "opens_at": (now - timedelta(days=1)).isoformat() + "Z",
            "closes_at": (now + timedelta(days=1)).isoformat() + "Z",
            "questions": [
                {
                    "type": "single",
                    "label": "Elegi una opcion",
                    "required": True,
                    "options": ["A", "B"],
                    "order_index": 1,
                }
            ],
        }
        created = self.client.post("/api/v2/surveys", json=payload, headers=self._auth())
        self.assertEqual(created.status_code, 201, created.get_json())
        survey_id = created.get_json()["id"]

        published = self.client.post(f"/api/v2/surveys/{survey_id}/publish", headers=self._auth())
        self.assertEqual(published.status_code, 200, published.get_json())
        token = published.get_json()["public_token"]

        public = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public.status_code, 200, public.get_json())
        published_payload = published.get_json()
        self._assert_admin_operations(published_payload, token, survey_id, tenant_slug=self.tenant.slug)
        question = public.get_json()["preguntas"][0]
        return survey_id, token, question["id"], question["opciones"][0]["id"]

    def _assert_admin_operations(self, payload, token, survey_id, tenant_slug=None):
        operations = payload["operations"]
        self.assertEqual(operations["contract_version"], "surveys.operations.v2")
        self.assertEqual(operations["survey_id"], survey_id)
        self.assertEqual(operations["public_token"], token)
        self.assertEqual(operations["tenant_slug"], tenant_slug)
        self.assertEqual(operations["admin_surface"]["id"], "survey_live_ops")
        self.assertIn(f"/admin/encuestas/{survey_id}/analytics", operations["admin_surface"]["frontend_path"])
        self.assertIn(f"survey_slug={token}", operations["admin_surface"]["frontend_path"])
        if tenant_slug:
            self.assertIn(f"tenant_slug={tenant_slug}", operations["admin_surface"]["frontend_path"])
        action_ids = {action["id"] for action in operations["admin_surface"]["actions"]}
        self.assertIn("open_live_results_admin", action_ids)
        self.assertIn("open_heatmap_admin", action_ids)
        self.assertIn("moderate_comments", action_ids)
        self.assertIn("share_whatsapp_qr", action_ids)
        self.assertIn("focus=heatmap", operations["analytics_surface"]["heatmap_route"])
        self.assertIn("include_heatmap=1", operations["analytics_surface"]["heatmap_route"])
        self.assertEqual(payload["admin_operations"], operations)

    def _post_public_response(self, endpoint, payload, submission_id, headers=None):
        request_headers = dict(headers or {})
        request_headers["Idempotency-Key"] = submission_id
        return self.client.post(
            endpoint,
            json={**payload, "submission_id": submission_id},
            headers=request_headers,
        )

    def test_vote_emits_realtime_and_updates_live_results(self):
        survey_id, token, question_id, option_id = self._create_live_vote()

        with patch("services.encuestas_service.emit_survey_update") as emit_update:
            response = self._post_public_response(
                f"/api/v2/public/surveys/{token}/respond",
                {
                    "anon_id": "flow-voter-1",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                "product-flow-voter-0001",
                {"X-Forwarded-For": "203.0.113.10"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        ack = response.get_json()
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["contract_version"], "surveys.public_response.v2")
        expected_live_url = f"/api/v2/public/surveys/{token}/live-results?tenant_slug={self.tenant.slug}"
        expected_room = f"encuesta:{self.tenant.slug}:{token}"
        self.assertEqual(ack["live_results_url"], expected_live_url)
        self.assertEqual(ack["public_state"]["status"], "live")
        self.assertEqual(ack["realtime"]["room"], expected_room)
        self.assertEqual(ack["realtime"]["socket"]["join_payload"], {"room": expected_room})
        event_names = {event["name"] for event in ack["realtime"]["socket"]["events"]}
        self.assertIn("survey_update_v2", event_names)
        self.assertIn("survey.vote.created", event_names)
        self.assertEqual(ack["links"]["qr_endpoint"], f"/api/public/encuestas/v1/{token}/qr?size=320")
        self.assertIn("download_qr", [step["id"] for step in ack["next_steps"]])
        self.assertIn("open_admin_analytics", [step["id"] for step in ack["next_steps"]])
        self.assertIn("open_heatmap_admin", [step["id"] for step in ack["next_steps"]])
        self._assert_admin_operations(ack, token, survey_id, tenant_slug=self.tenant.slug)

        emit_update.assert_called_once()
        self.assertEqual(emit_update.call_args.args[0], token)
        emitted_payload = emit_update.call_args.args[1]
        self.assertEqual(emitted_payload["contract_version"], "surveys.live_results.v2")
        self.assertEqual(emitted_payload["total_respuestas"], 1)
        self.assertEqual(emitted_payload["preguntas"][0]["total_votos"], 1)
        self.assertEqual(emitted_payload["preguntas"][0]["opciones"][0]["votos"], 1)
        self.assertIn("result_version", emitted_payload)
        self.assertIn("snapshot_version", emitted_payload)
        self.assertEqual(emitted_payload["legacy_results"]["total_respuestas"], 1)
        self.assertEqual(
            set(emitted_payload["event"]),
            {
                "contract_version",
                "event_id",
                "event_name",
                "tenant_id",
                "survey_id",
                "response_id",
                "slug",
            },
        )
        self.assertEqual(
            emitted_payload["event"]["contract_version"],
            "surveys.realtime_effect.v2",
        )
        self.assertEqual(
            emitted_payload["event"]["event_name"],
            "survey.response.committed",
        )
        self.assertEqual(emitted_payload["event"]["response_id"], ack["response_id"])
        self.assertEqual(emitted_payload["event"]["tenant_id"], self.tenant.id)

        analytics_event = AnalyticsEventV2.query.filter_by(
            tenant_id=self.tenant.id,
            event_name="vote_submitted",
        ).first()
        self.assertIsNotNone(analytics_event)
        self.assertEqual(analytics_event.channel, "web")
        self.assertEqual(analytics_event.entity_ref, f"survey:{emitted_payload['encuesta_id']}:response:{ack['response_id']}")
        self.assertEqual((analytics_event.metadata_payload or {}).get("response_id"), ack["response_id"])
        self.assertTrue((analytics_event.metadata_payload or {}).get("is_live_vote"))

        live = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0")
        self.assertEqual(live.status_code, 200, live.get_json())
        data = live.get_json()
        self.assertEqual(data["contract_version"], "surveys.live_results.v2")
        self.assertEqual(data["result_version"], emitted_payload["result_version"])
        self.assertEqual(data["total_respuestas"], 1)
        self.assertEqual(data["preguntas"][0]["total_votos"], 1)
        self.assertEqual(data["preguntas"][0]["opciones"][0]["votos"], 1)
        self.assertEqual(data["render_contract"]["preferred_visualization"], "live_vote_command_center")
        self.assertEqual(data["render_contract"]["product_surface"]["name"], "Noether Analytics Maps")
        self.assertEqual(data["render_contract"]["product_surface"]["scope"], "surveys_live_heatmap")
        self.assertIn("privacy_safe_geo_aggregation", data["render_contract"]["product_surface"]["supports"])
        self.assertEqual(data["realtime"]["room"], expected_room)
        self.assertEqual(data["realtime"]["polling"]["href"], expected_live_url)
        self.assertIn("admin_next_steps", data["render_contract"]["supports"])
        self.assertIn("admin_operations", data["render_contract"]["supports"])
        self._assert_admin_operations(data, token, survey_id, tenant_slug=self.tenant.slug)
        self.assertTrue(data["live_telemetry"]["has_responses"])

    def test_realtime_delivery_failure_keeps_vote_persisted_and_pollable(self):
        survey_id, token, question_id, option_id = self._create_live_vote()

        with patch(
            "services.encuestas_service.emit_survey_update",
            side_effect=RuntimeError("socket transport unavailable"),
        ) as emit_update:
            response = self._post_public_response(
                f"/api/v2/public/surveys/{token}/respond",
                {
                    "anon_id": "flow-voter-realtime-fallback",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                "product-flow-realtime-fallback-0001",
                {"X-Forwarded-For": "203.0.113.12"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 1)
        emit_update.assert_called_once()

        live = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0")
        self.assertEqual(live.status_code, 200, live.get_json())
        payload = live.get_json()
        self.assertEqual(payload["total_respuestas"], 1)
        self.assertEqual(payload["preguntas"][0]["total_votos"], 1)
        self.assertEqual(payload["preguntas"][0]["opciones"][0]["votos"], 1)

    def test_duplicate_question_entries_are_rejected_without_skewing_live_results(self):
        survey_id, token, question_id, first_option_id = self._create_live_vote()
        public = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public.status_code, 200, public.get_json())
        second_option_id = public.get_json()["preguntas"][0]["opciones"][1]["id"]

        with patch("services.encuestas_service.emit_survey_update") as emit_update:
            response = self._post_public_response(
                f"/api/v2/public/surveys/{token}/respond",
                {
                    "anon_id": "duplicate-question-voter",
                    "respuestas": [
                        {"pregunta_id": question_id, "opcion_id": first_option_id},
                        {"pregunta_id": question_id, "opcion_id": second_option_id},
                    ],
                },
                "product-flow-duplicate-question-0001",
                {"X-Forwarded-For": "203.0.113.11"},
            )

        self.assertEqual(response.status_code, 400, response.get_json())
        error = response.get_json()
        self.assertEqual(error["contract_version"], "surveys.public_response.v2")
        self.assertEqual(error["reason_code"], "duplicate_question_response")
        self.assertEqual(error["action_hint"], "merge_question_answers")
        self.assertEqual(error["question_id"], question_id)
        self.assertFalse(error["retryable"])
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 0)
        emit_update.assert_not_called()

        live = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0")
        self.assertEqual(live.status_code, 200, live.get_json())
        live_payload = live.get_json()
        self.assertEqual(live_payload["total_respuestas"], 0)
        self.assertEqual(live_payload["preguntas"][0]["total_votos"], 0)

    def test_live_results_with_heatmap_returns_privacy_safe_vote_coordinates(self):
        survey_id, token, question_id, option_id = self._create_live_vote()

        response = self._post_public_response(
            f"/api/v2/public/surveys/{token}/respond",
            {
                "anon_id": "flow-voter-geo-1",
                "source": "whatsapp_webview",
                "lat": -33.08149,
                "lng": -68.46849,
                "barrio": "Centro",
                "ciudad": "Junin",
                "provincia": "Mendoza",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
            "product-flow-geo-voter-0001",
            {"X-Forwarded-For": "203.0.113.30"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())

        live = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=1")
        self.assertEqual(live.status_code, 200, live.get_json())
        data = live.get_json()
        self.assertEqual(data["contract_version"], "surveys.live_results.v2")
        self.assertEqual(data["realtime"]["room"], f"encuesta:{self.tenant.slug}:{token}")
        self.assertIn("heatmap", data["render_contract"]["supports"])
        self.assertEqual(data["render_contract"]["product_surface"]["name"], "Noether Analytics Maps")
        self.assertEqual(data["heatmap"]["enabled"], True)
        self.assertGreaterEqual(len(data["heatmap"]["points"]), 1)
        self.assertGreaterEqual(len(data["heatmap"]["cells"]), 1)
        point = data["heatmap"]["points"][0]
        self.assertEqual(point["privacy_mode"], "public_aggregated")
        self.assertEqual(point["source"], "survey_heatmap_cell")
        self.assertEqual(point["lat"], round(point["lat"], 3))
        self.assertEqual(point["lng"], round(point["lng"], 3))
        self.assertNotEqual(point["lat"], -33.08149)
        self.assertNotEqual(point["lng"], -68.46849)
        self.assertNotIn("submitted_at", point)
        self.assertEqual(point["barrio"], "Centro")
        self.assertEqual(data["heatmap"]["metadata"]["points_count"], 1)
        self.assertEqual(data["heatmap"]["metadata"]["cells_count"], 1)
        self.assertEqual(data["heatmap"]["metadata"]["privacy_mode"], "public_aggregated")
        self.assertTrue(data["heatmap"]["metadata"]["raw_points_redacted"])
        self.assertEqual(data["heatmap"]["metadata"]["coordinate_precision"], "rounded_3_decimals")
        self.assertEqual(data["heatmap"]["metadata"]["raw_points_count"], 1)
        self._assert_admin_operations(data, token, survey_id, tenant_slug=self.tenant.slug)

    def test_live_results_apply_explicit_analytics_range_to_all_surfaces(self):
        survey_id, token, question_id, option_id = self._create_live_vote()
        fixed_now = datetime(2026, 7, 12, 15, 0, tzinfo=timezone.utc)

        for index, (lat, lng) in enumerate(((-33.08149, -68.46849), (-33.09149, -68.47849)), start=1):
            response = self._post_public_response(
                f"/api/v2/public/surveys/{token}/respond",
                {
                    "anon_id": f"range-voter-{index}",
                    "source": "web",
                    "lat": lat,
                    "lng": lng,
                    "barrio": "Centro",
                    "ciudad": "Junin",
                    "provincia": "Mendoza",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                f"product-flow-range-voter-{index:04d}",
                {"X-Forwarded-For": f"203.0.113.{40 + index}"},
            )
            self.assertEqual(response.status_code, 201, response.get_json())

        respuestas = EncRespuesta.query.filter_by(encuesta_id=survey_id).order_by(EncRespuesta.id.asc()).all()
        self.assertEqual(len(respuestas), 2)
        respuestas[0].submitted_at = fixed_now - timedelta(hours=2)
        respuestas[1].submitted_at = fixed_now - timedelta(minutes=30)
        db.session.commit()

        def fetch_v2(preset: str):
            query = urlencode(
                {
                    "include_heatmap": 1,
                    "range_preset": preset,
                    "range_timezone": "America/Argentina/Buenos_Aires",
                    "momentum_window_minutes": 60,
                }
            )
            response = self.client.get(f"/api/v2/public/surveys/{token}/live-results?{query}")
            self.assertEqual(response.status_code, 200, response.get_json())
            return response.get_json()

        def assert_consistent(payload, expected_total: int):
            self.assertEqual(payload["total_respuestas"], expected_total)
            self.assertEqual(payload["preguntas"][0]["total_votos"], expected_total)
            self.assertEqual(payload["preguntas"][0]["opciones"][0]["votos"], expected_total)
            self.assertEqual(sum(point["total"] for point in payload["timeline_minute"]), expected_total)
            self.assertEqual(payload["heatmap"]["metadata"]["raw_points_count"], expected_total)

        with patch("services.encuestas_analytics_service._utc_now", return_value=fixed_now):
            last_60m = fetch_v2("last_60m")
            assert_consistent(last_60m, 1)
            self.assertEqual(last_60m["analytics_range"]["preset"], "last_60m")
            self.assertEqual(last_60m["analytics_range"]["label"], "Últimos 60 minutos")
            self.assertEqual(last_60m["analytics_range"]["desde"], "2026-07-12T14:00:00+00:00")
            self.assertEqual(last_60m["momentum"]["window_minutes"], 30)

            last_24h = fetch_v2("last_24h")
            assert_consistent(last_24h, 2)
            self.assertEqual(last_24h["analytics_range"]["preset"], "last_24h")
            self.assertEqual(last_24h["analytics_range"]["label"], "Últimas 24 horas")
            self.assertEqual(last_24h["analytics_range"]["desde"], "2026-07-11T15:00:00+00:00")

            today = fetch_v2("today")
            assert_consistent(today, 2)
            self.assertEqual(today["analytics_range"]["preset"], "today")
            self.assertEqual(today["analytics_range"]["label"], "Hoy (desde las 00:00)")
            self.assertEqual(today["analytics_range"]["timezone"], "America/Argentina/Buenos_Aires")
            self.assertEqual(today["analytics_range"]["desde"], "2026-07-12T03:00:00+00:00")

            custom_query = urlencode(
                {
                    "include_heatmap": 1,
                    "desde": (fixed_now - timedelta(minutes=90)).isoformat(),
                    "hasta": fixed_now.isoformat(),
                    "range_timezone": "America/Argentina/Buenos_Aires",
                    "momentum_window_minutes": 10,
                }
            )
            custom_response = self.client.get(
                f"/api/public/encuestas/v1/{token}/live-results?{custom_query}"
            )
            self.assertEqual(custom_response.status_code, 200, custom_response.get_json())
            custom = custom_response.get_json()
            assert_consistent(custom, 1)
            self.assertEqual(custom["analytics_range"]["mode"], "custom")
            self.assertIsNone(custom["analytics_range"]["preset"])

            ambiguous_query = urlencode(
                {
                    "range_preset": "last_60m",
                    "desde": (fixed_now - timedelta(minutes=90)).isoformat(),
                    "hasta": fixed_now.isoformat(),
                }
            )
            ambiguous_response = self.client.get(
                f"/api/v2/public/surveys/{token}/live-results?{ambiguous_query}"
            )
            self.assertEqual(ambiguous_response.status_code, 400, ambiguous_response.get_json())
            self.assertEqual(
                ambiguous_response.get_json()["reason_code"],
                "ambiguous_analytics_range",
            )

    def test_pwa_survey_response_matches_realtime_contract_for_whatsapp_webview(self):
        survey_id, token, question_id, option_id = self._create_live_vote()

        response = self._post_public_response(
            f"/api/pwa/public/surveys/{token}/respond?tenant={self.tenant.slug}",
            {
                "anon_id": "flow-voter-pwa-1",
                "source": "whatsapp_webview",
                "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
            },
            "product-flow-pwa-voter-0001",
            {"X-Forwarded-For": "203.0.113.20"},
        )

        self.assertEqual(response.status_code, 201, response.get_json())
        ack = response.get_json()
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["contract_version"], "surveys.public_response.v2")
        self.assertEqual(ack["id"], ack["response_id"])
        self.assertEqual(ack["public_state"]["status"], "live")
        self.assertEqual(
            ack["live_results_url"],
            f"/api/v2/public/surveys/{token}/live-results?tenant_slug={self.tenant.slug}",
        )
        self.assertEqual(ack["realtime"]["room"], f"encuesta:{self.tenant.slug}:{token}")
        self.assertEqual(ack["realtime"]["legacy_room"], f"encuesta:{self.tenant.slug}:{token}")
        self.assertEqual(ack["realtime"]["rooms"], [f"encuesta:{self.tenant.slug}:{token}"])
        event_names = {event["name"] for event in ack["realtime"]["socket"]["events"]}
        self.assertIn("survey_update_v2", event_names)
        self.assertIn("survey.vote.created", event_names)
        self.assertEqual(ack["links"]["qr_endpoint"], f"/api/public/encuestas/v1/{token}/qr?size=320")
        self.assertEqual(ack["runtime"]["flow_id"], "survey_vote")
        self.assertEqual(ack["runtime"]["action_id"], "survey_response")
        self.assertIn("open_live_results", [action["id"] for action in ack["ui_actions"]])
        self.assertIn("download_qr", [step["id"] for step in ack["next_steps"]])
        self.assertIn("open_admin_analytics", [step["id"] for step in ack["next_steps"]])
        self._assert_admin_operations(ack, token, survey_id, tenant_slug=self.tenant.slug)
