import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from config import TestConfig
from models import db, EncComentario, EncEncuesta, TenantProfile, User
from services.encuestas_service import (
    create_comentario,
    EncuestaError,
    issue_social_comment_token,
    list_comentarios,
    verify_social_comment_token,
)


class EncuestasSocialCommentsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app.config["SURVEY_COMMENT_SOCIAL_PROVIDERS"] = [
            {"id": provider, "label": provider.title(), "oauthUrl": f"https://auth.example.com/{provider}", "messageOrigin": "https://auth.example.com"}
            for provider in ("facebook", "google", "instagram")
        ]
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            email="survey-social-owner@example.com",
            name="Survey social owner",
            rol="admin",
            tipo_chat="municipio",
        )
        self.owner.set_password("survey-social-test-only")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="survey-social-test",
            nombre="Survey social test",
            tipo="municipio",
            municipio_id=self.owner.id,
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.owner.tenant_id = self.tenant.id
        self.app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = self.tenant.id

        self.encuesta = EncEncuesta(
            tenant_id=self.tenant.id,
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

        payload["social_token"] = issue_social_comment_token({"provider": payload["auth_provider"], **payload})
        comentario = create_comentario(self.encuesta.id, payload, user=None)

        self.assertEqual(comentario.nombre_autor, "Marcelo Perez")
        self.assertEqual(comentario.anon_id, "social:instagram:ig_12345")

        listado = list_comentarios(self.encuesta.id, limit=10, offset=0)
        self.assertEqual(len(listado), 1)
        self.assertEqual(listado[0]["comment_mode"], "social")
        self.assertEqual(listado[0]["auth_provider"], "instagram")
        self.assertNotIn("auth_user_id", listado[0])
        self.assertNotIn("anon_id", listado[0])
        self.assertNotIn("user_id", listado[0])

    def test_comment_list_exposes_only_consented_profile_avatar(self):
        user = User(
            email="avatar-survey@test.com",
            name="Marcelo Avatar",
            password_hash="hash",
            accesibilidad={
                "identity": {
                    "avatar_url": "https://cdn.example.com/profile/marcelo.webp",
                    "avatar_source": "profile_upload",
                    "avatar_consent": True,
                }
            },
        )
        db.session.add(user)
        db.session.commit()

        create_comentario(
            self.encuesta.id,
            {"texto": "Comentario con perfil", "mode": "social", "social_token": issue_social_comment_token({"provider": "google", "auth_user_id": "avatar-verified", "auth_first_name": "Marcelo", "auth_last_name": "Avatar"})},
            user=user,
        )

        listado = list_comentarios(self.encuesta.id, limit=10, offset=0)

        self.assertEqual(listado[0]["avatar_url"], "https://cdn.example.com/profile/marcelo.webp")
        self.assertEqual(listado[0]["picture"], "https://cdn.example.com/profile/marcelo.webp")
        self.assertEqual(listado[0]["avatar_source"], "profile_upload")
        self.assertTrue(listado[0]["avatar_consent"])
        self.assertTrue(listado[0]["profile_picture_consent"])

    def test_comment_list_hides_profile_avatar_without_consent(self):
        user = User(
            email="avatar-hidden@test.com",
            name="Avatar Hidden",
            password_hash="hash",
            accesibilidad={
                "identity": {
                    "avatar_url": "https://cdn.example.com/profile/hidden.webp",
                    "avatar_source": "profile_upload",
                    "avatar_consent": False,
                }
            },
        )
        db.session.add(user)
        db.session.commit()

        create_comentario(
            self.encuesta.id,
            {"texto": "Comentario sin consentimiento"},
            user=user,
        )

        listado = list_comentarios(self.encuesta.id, limit=10, offset=0)

        self.assertIsNone(listado[0]["avatar_url"])
        self.assertIsNone(listado[0]["picture"])
        self.assertFalse(listado[0]["avatar_consent"])

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
        self.assertTrue(cfg.get("requiresSocialToken"))

    @patch("services.encuestas_service.analytics_ingestor", new_callable=MagicMock)
    def test_create_social_comment_emits_analytics_events(self, mock_ingestor):
        payload = {
            "texto": "Me gustó la propuesta",
            "mode": "social",
            "previous_mode": "anon",
            "auth_provider": "google",
            "auth_user_id": "g_7788",
        }
        payload["social_token"] = issue_social_comment_token({"provider": "google", "auth_user_id": "g_7788"})
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
        self.assertNotIn("auth_user_id", comentario)
        self.assertNotIn("anon_id", comentario)
        self.assertNotIn("user_id", comentario)

    @patch("services.encuestas_service.emit_survey_comment")
    def test_public_comment_rest_and_socket_omit_correlation_identifiers(self, emit_mock):
        sentinel = "social-private-correlation-7788"
        comentario = create_comentario(
            self.encuesta.id,
            {
                "texto": "Comentario público sin identificadores persistentes",
                "mode": "social",
                "auth_provider": "google",
                "auth_user_id": sentinel,
                "auth_first_name": "Ana",
                "social_token": issue_social_comment_token({"provider": "google", "auth_user_id": sentinel, "auth_first_name": "Ana"}),
            },
            user=None,
        )
        self.assertIn(sentinel, comentario.anon_id)

        response = self.client.get(
            f"/api/public/encuestas/{self.encuesta.slug}/comentarios"
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        public_comment = (response.get_json() or [])[0]
        emitted = emit_mock.call_args.args[1]

        for payload in (public_comment, emitted):
            self.assertNotIn("user_id", payload)
            self.assertNotIn("anon_id", payload)
            self.assertNotIn("auth_user_id", payload)
            self.assertNotIn(sentinel, str(payload))
            self.assertEqual(payload.get("comment_mode"), "social")
            self.assertEqual(payload.get("auth_provider"), "google")

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

    def test_anonymous_comment_discards_bearer_identity_and_declared_profile(self):
        with patch("routes.encuestas_public.user_from_token", return_value=self.owner):
            response = self.client.post(
                f"/api/public/encuestas/{self.encuesta.slug}/comentarios",
                headers={"Authorization": "Bearer opaque-session-fixture"},
                json={"texto": "Opinión anónima", "modo": "anonimo", "nombre": "Filtrar nombre", "anon_id": "private-correlation", "auth_user_id": "claimed-id"},
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        stored = db.session.get(EncComentario, response.get_json()["comentario"]["id"])
        self.assertIsNone(stored.user_id)
        self.assertIsNone(stored.anon_id)
        self.assertIsNone(stored.nombre_autor)
        body = response.get_json()["comentario"]
        self.assertEqual(body["nombre_autor"], "Anónimo")
        self.assertIsNone(body["avatar_url"])
        self.assertNotIn(self.owner.name, str(body))

    def test_social_aliases_require_signed_token_even_when_legacy_flag_is_false(self):
        self.app.config["SURVEY_SOCIAL_COMMENT_REQUIRE_TOKEN"] = False
        for key, mode in (("mode", "social"), ("modo", "google"), ("comment_mode", "facebook")):
            with self.subTest(key=key, mode=mode):
                response = self.client.post(f"/api/public/encuestas/{self.encuesta.slug}/comentarios", json={"texto": "Identidad declarada", key: mode, "auth_provider": "google", "auth_user_id": "forged"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["reason_code"], "social_token_required")
        self.assertEqual(EncComentario.query.count(), 0)

    def test_service_rejects_unsigned_social_identity(self):
        with self.assertRaises(EncuestaError) as failure:
            create_comentario(self.encuesta.id, {"texto": "Directa", "mode": "social", "auth_provider": "google", "auth_user_id": "forged"}, None)
        self.assertEqual(failure.exception.payload["reason_code"], "social_token_required")

    def test_social_mode_alias_uses_signed_profile_and_rejects_tampered_token(self):
        token = issue_social_comment_token({"provider": "google", "auth_user_id": "g-confirmed", "auth_first_name": "Ana"})
        response = self.client.post(f"/api/public/encuestas/{self.encuesta.slug}/comentarios", json={"texto": "Firmada", "modo": "google", "nombre": "Nombre inventado", "social_token": token})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["comentario"]["nombre_autor"], "Ana")
        response = self.client.post(f"/api/public/encuestas/{self.encuesta.slug}/comentarios", json={"texto": "No firmada", "mode": "social", "social_token": token + "tampered"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason_code"], "invalid_social_token")

    def test_comment_limit_and_pagination_are_bounded(self):
        endpoint = f"/api/public/encuestas/{self.encuesta.slug}/comentarios"
        response = self.client.post(endpoint, json={"texto": "x" * 501, "mode": "anon"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["reason_code"], "comment_too_long")
        comment = create_comentario(self.encuesta.id, {"texto": "Límite válido", "mode": "anon"}, None)
        self.assertEqual(list_comentarios(self.encuesta.id, limit=-1, offset=-3)[0]["id"], comment.id)
        with patch("routes.encuestas_public.list_comentarios", wraps=list_comentarios):
            self.assertEqual(self.client.get(endpoint + "?limit=999999&offset=-7").status_code, 200)

    def test_report_cannot_target_comment_from_another_tenant(self):
        foreign_tenant = TenantProfile(slug="foreign-comments", nombre="Foreign", tipo="pyme", pyme_id=self.owner.id)
        db.session.add(foreign_tenant)
        db.session.flush()
        foreign = EncEncuesta(tenant_id=foreign_tenant.id, slug="foreign-survey", titulo="Foreign", tipo="opinion", estado="publicada", permitir_comentarios=True)
        db.session.add(foreign)
        db.session.flush()
        comment = EncComentario(encuesta_id=foreign.id, texto="Comentario ajeno", estado="publicado", report_count=0)
        db.session.add(comment)
        db.session.commit()
        response = self.client.post(f"/api/public/encuestas/{self.encuesta.slug}/comentarios/{comment.id}/reportar")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(db.session.get(EncComentario, comment.id).report_count, 0)

    def test_no_social_provider_is_advertised_without_a_usable_authenticated_flow(self):
        self.app.config["SURVEY_COMMENT_SOCIAL_PROVIDERS"] = [{"id": "google", "label": "Google"}]
        payload = self.client.get(f"/api/public/encuestas/{self.encuesta.slug}").get_json()
        self.assertEqual(payload["socialProviders"], [])
        self.assertEqual(payload["commentConfig"]["socialProviders"], [])
        self.assertEqual(payload["commentConfig"]["acceptedModes"], ["anon"])
