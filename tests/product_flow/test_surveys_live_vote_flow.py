import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User
from routes.v2.surveys import _public_response_rate_buckets


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
        _public_response_rate_buckets.clear()
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
        _public_response_rate_buckets.clear()
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
        question = public.get_json()["preguntas"][0]
        return token, question["id"], question["opciones"][0]["id"]

    def test_vote_emits_realtime_and_updates_live_results(self):
        token, question_id, option_id = self._create_live_vote()

        with patch("services.encuestas_service.emit_survey_update") as emit_update:
            response = self.client.post(
                f"/api/v2/public/surveys/{token}/respond",
                json={
                    "anon_id": "flow-voter-1",
                    "source": "web",
                    "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
                },
                headers={"X-Forwarded-For": "203.0.113.10"},
            )

        self.assertEqual(response.status_code, 201, response.get_json())
        ack = response.get_json()
        self.assertTrue(ack["ok"])
        self.assertEqual(ack["contract_version"], "surveys.public_response.v2")
        self.assertEqual(ack["live_results_url"], f"/api/v2/public/surveys/{token}/live-results")
        self.assertEqual(ack["public_state"]["status"], "live")
        self.assertEqual(ack["realtime"]["room"], f"encuesta_{token}")
        self.assertEqual(ack["realtime"]["socket"]["join_payload"], {"room": f"encuesta_{token}"})
        self.assertEqual(ack["realtime"]["socket"]["events"][0]["name"], "survey_update_v2")
        self.assertEqual(ack["links"]["qr_endpoint"], f"/api/public/encuestas/v1/{token}/qr?size=320")
        self.assertIn("download_qr", [step["id"] for step in ack["next_steps"]])

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

        live = self.client.get(f"/api/v2/public/surveys/{token}/live-results?include_heatmap=0")
        self.assertEqual(live.status_code, 200, live.get_json())
        data = live.get_json()
        self.assertEqual(data["contract_version"], "surveys.live_results.v2")
        self.assertEqual(data["result_version"], emitted_payload["result_version"])
        self.assertEqual(data["total_respuestas"], 1)
        self.assertEqual(data["preguntas"][0]["total_votos"], 1)
        self.assertEqual(data["preguntas"][0]["opciones"][0]["votos"], 1)
        self.assertEqual(data["render_contract"]["preferred_visualization"], "live_vote_command_center")
        self.assertEqual(data["realtime"]["room"], f"encuesta_{token}")
        self.assertEqual(data["realtime"]["polling"]["href"], f"/api/v2/public/surveys/{token}/live-results")
        self.assertIn("admin_next_steps", data["render_contract"]["supports"])
        self.assertTrue(data["live_telemetry"]["has_responses"])
