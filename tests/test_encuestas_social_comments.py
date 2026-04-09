import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from config import TestConfig
from models import db, EncEncuesta
from services.encuestas_service import (
    create_comentario,
    issue_social_comment_token,
    list_comentarios,
    verify_social_comment_token,
)


class EncuestasSocialCommentsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config["SURVEY_COMMENT_SOCIAL_PROVIDERS"] = [
            {"id": "facebook", "label": "Facebook"},
            {"id": "google", "label": "Google"},
            {"id": "instagram", "label": "Instagram"},
        ]
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.encuesta = EncEncuesta(
            tenant_id=1,
            slug="encuesta-social-test",
            titulo="Encuesta social",
            descripcion="desc",
            tipo="opinion",
            estado="publicada",
            permitir_comentarios=True,
            anonimo_permitido=True,
        )
        db.session.add(self.encuesta)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_create_social_comment_persists_identity_tag(self):
        payload = {
            "texto": "Excelente iniciativa",
            "mode": "social",
            "auth_provider": "instagram",
            "auth_user_id": "ig_12345",
            "auth_first_name": "Marcelo",
            "auth_last_name": "Perez",
            "auth_email": "marcelo@example.com",
        }

        comentario = create_comentario(self.encuesta.id, payload, user=None)

        self.assertEqual(comentario.nombre_autor, "Marcelo Perez")
        self.assertEqual(comentario.anon_id, "social:instagram:ig_12345")

        listado = list_comentarios(self.encuesta.id, limit=10, offset=0)
        self.assertEqual(len(listado), 1)
        self.assertEqual(listado[0]["comment_mode"], "social")
        self.assertEqual(listado[0]["auth_provider"], "instagram")
        self.assertEqual(listado[0]["auth_user_id"], "ig_12345")

    def test_public_survey_payload_includes_social_providers(self):
        response = self.client.get(f"/api/public/encuestas/{self.encuesta.slug}")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        social = payload.get("socialProviders") or []
        provider_ids = {item.get("id") for item in social if isinstance(item, dict)}
        self.assertIn("facebook", provider_ids)
        self.assertIn("google", provider_ids)
        self.assertIn("instagram", provider_ids)
        cfg = payload.get("commentConfig") or {}
        self.assertIn("acceptedModes", cfg)
        self.assertEqual(cfg.get("acceptedModes"), ["anon", "social"])
        self.assertFalse(cfg.get("requiresSocialToken"))

    @patch("services.encuestas_service.analytics_ingestor", new_callable=MagicMock)
    def test_create_social_comment_emits_analytics_events(self, mock_ingestor):
        payload = {
            "texto": "Me gustó la propuesta",
            "mode": "social",
            "previous_mode": "anon",
            "auth_provider": "google",
            "auth_user_id": "g_7788",
        }
        create_comentario(self.encuesta.id, payload, user=None)

        event_names = [call.kwargs.get("event_name") for call in mock_ingestor.track.call_args_list]
        self.assertIn("survey_comment_mode_changed", event_names)
        self.assertIn("survey_comment_submitted", event_names)

    def test_social_comment_token_roundtrip(self):
        token = issue_social_comment_token(
            {
                "provider": "Google",
                "auth_user_id": "g_abc",
                "auth_email": "test@example.com",
                "auth_first_name": "Ana",
                "auth_last_name": "Gomez",
            }
        )
        decoded = verify_social_comment_token(token, max_age_seconds=300)
        self.assertIsInstance(decoded, dict)
        self.assertEqual(decoded.get("provider"), "google")
        self.assertEqual(decoded.get("auth_user_id"), "g_abc")
        self.assertEqual(decoded.get("auth_email"), "test@example.com")

    def test_social_comment_post_accepts_valid_social_token(self):
        token = issue_social_comment_token(
            {
                "provider": "facebook",
                "auth_user_id": "fb_5566",
                "auth_first_name": "Laura",
                "auth_last_name": "Sosa",
            }
        )
        response = self.client.post(
            f"/api/public/encuestas/{self.encuesta.slug}/comentarios",
            json={"texto": "Comentario con token", "social_token": token},
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json() or {}
        comentario = body.get("comentario") or {}
        self.assertEqual(comentario.get("comment_mode"), "social")
        self.assertEqual(comentario.get("auth_provider"), "facebook")
        self.assertEqual(comentario.get("auth_user_id"), "fb_5566")

    def test_social_comment_rejects_mismatched_payload_and_token(self):
        token = issue_social_comment_token(
            {
                "provider": "google",
                "auth_user_id": "g_100",
            }
        )
        response = self.client.post(
            f"/api/public/encuestas/{self.encuesta.slug}/comentarios",
            json={
                "texto": "Comentario",
                "mode": "social",
                "social_token": token,
                "auth_provider": "facebook",
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("reason_code"), "social_identity_mismatch")

    def test_social_comment_can_require_token_via_config(self):
        self.app.config["SURVEY_SOCIAL_COMMENT_REQUIRE_TOKEN"] = True
        config_response = self.client.get(f"/api/public/encuestas/{self.encuesta.slug}")
        self.assertEqual(config_response.status_code, 200)
        config_payload = config_response.get_json() or {}
        cfg = config_payload.get("commentConfig") or {}
        self.assertTrue(cfg.get("requiresSocialToken"))

        response = self.client.post(
            f"/api/public/encuestas/{self.encuesta.slug}/comentarios",
            json={
                "texto": "Comentario",
                "mode": "social",
                "auth_provider": "google",
                "auth_user_id": "g_11",
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("reason_code"), "social_token_required")
