import os
import unittest
from datetime import datetime, timedelta

import jwt

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


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
        self.tenant_1 = TenantProfile(slug="tenant-surv-a", nombre="Tenant Survey A", tipo="municipio", pyme_id=self.admin_1.id)
        db.session.add(self.tenant_1)
        db.session.commit()
        self.admin_1.tenant_id = self.tenant_1.id
        db.session.add(self.admin_1)

        self.admin_2 = self._create_user("admin-survey-b@test.com", "admin", "tenant-surv-b")
        self.tenant_2 = TenantProfile(slug="tenant-surv-b", nombre="Tenant Survey B", tipo="pyme", pyme_id=self.admin_2.id)
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
        token = publish_resp.get_json().get("public_token")
        self.assertTrue(token)

        public_get = self.client.get(f"/api/v2/public/surveys/{token}")
        self.assertEqual(public_get.status_code, 200)

        question_id = public_get.get_json().get("preguntas", [])[0].get("id")
        option_id = public_get.get_json().get("preguntas", [])[0].get("opciones", [])[0].get("id")

        respond_payload = {
            "anon_id": "anon-survey-1",
            "source": "web",
            "respuestas": [{"pregunta_id": question_id, "opcion_id": option_id}],
        }
        respond_resp = self.client.post(f"/api/v2/public/surveys/{token}/respond", json=respond_payload)
        self.assertEqual(respond_resp.status_code, 201)
        self.assertTrue(respond_resp.get_json().get("ok"))

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

    def test_tenant_isolation_on_list(self):
        headers_1 = {**self._auth(self.admin_1), "X-Tenant-Slug": self.tenant_1.slug}
        headers_2 = {**self._auth(self.admin_2), "X-Tenant-Slug": self.tenant_2.slug}

        self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers_1)
        self.client.post("/api/v2/surveys", json=self._create_payload(), headers=headers_2)

        listed = self.client.get("/api/v2/surveys", headers=headers_1)
        self.assertEqual(listed.status_code, 200)
        items = listed.get_json().get("items") or []
        self.assertTrue(items)
        self.assertTrue(all(item.get("tenant_id") == self.tenant_1.id for item in items))


if __name__ == "__main__":
    unittest.main()
