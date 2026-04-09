import unittest

from app import create_app
from config import TestConfig
from models import db, EncEncuesta
from services.encuestas_service import create_comentario, list_comentarios


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

