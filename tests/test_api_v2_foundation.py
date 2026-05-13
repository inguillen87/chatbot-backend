import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import TenantProfile, User


class V2BaseTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class BadProdConfig(V2BaseTestConfig):
    ENV = "prod"
    SECRET_KEY = "una-llave-secreta-muy-segura-para-desarrollo-local"
    DEBUG = False


class ApiV2FoundationTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(V2BaseTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_v2_health_ok(self):
        resp = self.client.get("/api/v2/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "version": "v2"})

    def test_v2_demo_catalog_does_not_expose_sensitive_keys(self):
        resp = self.client.get("/api/v2/demo/catalog")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.catalog.v2")
        self.assertEqual(payload.get("sectors"), ["gobierno", "empresas", "educacion"])
        self.assertEqual(payload.get("pillar_contract_version"), "demo.pillars.v1")
        self.assertTrue(any(pillar.get("key") == "empresas" for pillar in payload.get("pillars") or []))
        self.assertIn("rubros", payload)
        self.assertIn("sector_groups", payload)
        self.assertTrue(any(group.get("key") == "educacion" for group in payload.get("sector_groups") or []))
        sector_groups = {group.get("key"): group for group in payload.get("sector_groups") or []}
        self.assertEqual((sector_groups.get("gobierno") or {}).get("tenant_slug"), "municipio")
        self.assertEqual((sector_groups.get("empresas") or {}).get("tenant_slug"), "bodega")
        self.assertEqual((sector_groups.get("educacion") or {}).get("tenant_slug"), "colegio-demo")
        self.assertTrue(any((item.get("resources") or []) for item in payload.get("rubros") or []))
        self.assertTrue(payload.get("resources"))

        flattened = str(payload).lower()
        self.assertNotIn("password", flattened)
        self.assertNotIn("token", flattened)
        self.assertNotIn("secret", flattened)

    def test_v2_demo_session_returns_workspace_contract(self):
        owner = User(name="Demo Pyme", email="demo-pyme@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="demo-pyme", nombre="Demo Pyme", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "tenant_slug": tenant.slug},
            headers={"X-Request-Id": "demo-contract-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        self.assertEqual(payload.get("contract_version"), "demo.session.v2")
        self.assertEqual(payload.get("tenant_slug"), tenant.slug)
        self.assertEqual(payload.get("request_id"), "demo-contract-1")
        self.assertEqual(resp.headers.get("X-Request-Id"), "demo-contract-1")
        self.assertEqual(workspace.get("title"), tenant.nombre)
        self.assertIsInstance(workspace.get("quick_replies"), list)
        self.assertTrue(all(isinstance(item, dict) and item.get("label") for item in workspace.get("quick_replies")))
        self.assertIsInstance(workspace.get("value_cards"), list)
        self.assertIsInstance(workspace.get("handoff_labels"), dict)
        self.assertIn("createTicket", workspace.get("handoff_labels"))
        self.assertIsInstance(workspace.get("first_visit"), dict)
        self.assertTrue((workspace.get("first_visit") or {}).get("headline"))
        self.assertIsInstance(workspace.get("sample_conversations"), list)
        self.assertTrue(workspace.get("sample_conversations"))
        self.assertTrue((workspace.get("media_capabilities") or {}).get("input_modes"))
        self.assertTrue((workspace.get("conversion_ctas") or {}).get("actions"))
        self.assertEqual((workspace.get("animation_tokens") or {}).get("version"), "chat.motion.v1")
        self.assertIsInstance(payload.get("experience_blueprint"), dict)
        self.assertEqual(payload["experience_blueprint"].get("version"), "2026-05-agent-experience-v2")
        self.assertTrue((payload.get("lead_capture") or {}).get("endpoint"))
        self.assertEqual(
            ((payload.get("media_capabilities") or {}).get("input_modes") or {}).get("audio", {}).get("multipart_field"),
            "audio_file",
        )
        self.assertTrue(any(item.get("intent") == "crear_pedido" for item in ((payload.get("conversion_ctas") or {}).get("actions") or [])))
        chat_bootstrap = payload.get("chat_bootstrap") or {}
        self.assertEqual(chat_bootstrap.get("contract_version"), "demo.chat_bootstrap.v1")
        self.assertEqual(chat_bootstrap.get("endpoint"), "/ask/pyme")
        self.assertEqual(chat_bootstrap.get("same_origin_endpoint"), "/api/ask/pyme")
        self.assertEqual(chat_bootstrap.get("fallback_endpoint"), "/ask")
        self.assertEqual(chat_bootstrap.get("response_contract"), "chat.response.v1")
        self.assertTrue((chat_bootstrap.get("runtime_contract") or {}).get("server_side_ai"))
        self.assertFalse((chat_bootstrap.get("runtime_contract") or {}).get("frontend_llm_keys_allowed"))
        self.assertEqual((chat_bootstrap.get("headers") or {}).get("X-Chat-Session-Id"), payload.get("demo_session_id"))
        self.assertEqual((chat_bootstrap.get("headers") or {}).get("X-Tenant-Slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("query") or {}).get("tenant_slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("tipo_chat"), "pyme")
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("tenant_slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("demo_mode"), True)
        self.assertEqual((workspace.get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
        self.assertEqual(((payload.get("chat_seed") or {}).get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
        self.assertTrue((chat_bootstrap.get("supports") or {}).get("audio"))
        self.assertTrue((chat_bootstrap.get("supports") or {}).get("image"))
        self.assertEqual((workspace.get("runtime_contract") or {}).get("chat_response_contract"), "chat.response.v1")
        self.assertFalse((workspace.get("runtime_contract") or {}).get("local_mock_allowed"))
        self.assertTrue(workspace.get("allowed_actions"))
        self.assertEqual((workspace.get("tracking") or {}).get("contract_version"), "demo.tracking.v1")
        self.assertIn("/api/v2/demo/admin-preview", workspace.get("admin_preview_endpoint") or "")

    def test_demo_session_canonical_and_legacy_aliases_delegate_to_v2_with_cors(self):
        owner = User(name="Colegio Demo", email="colegio-compat@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="colegio-demo",
            nombre="Colegio Demo",
            tipo="pyme",
            pyme_id=owner.id,
            vertical="educacion",
        )
        db.session.add(tenant)
        db.session.commit()

        for path in ("/api/v2/demo/session", "/v2/demo/session", "/api/v1/demo/session", "/v1/demo/session"):
            options = self.client.open(
                path,
                method="OPTIONS",
                headers={"Origin": "https://www.chatboc.ar"},
            )
            self.assertEqual(options.status_code, 200)
            self.assertEqual(options.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")
            self.assertIn("X-Request-Id", options.headers.get("Access-Control-Expose-Headers", ""))

            resp = self.client.post(
                path,
                json={"sector": "educacion", "tenant_slug": "colegio-demo", "rubro": "colegio-demo"},
                headers={"Origin": "https://www.chatboc.ar"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            workspace = payload.get("workspace") or {}
            self.assertEqual(payload.get("contract_version"), "demo.session.v2")
            self.assertEqual(payload.get("tenant_slug"), "colegio-demo")
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")
            self.assertIn("request_id", payload)
            self.assertEqual((workspace.get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
            self.assertEqual(((payload.get("chat_bootstrap") or {}).get("headers") or {}).get("X-Tenant-Slug"), tenant.slug)

    def test_v2_demo_admin_preview_returns_sector_contract(self):
        resp = self.client.get("/api/v2/demo/admin-preview?sector=educacion&tenant_slug=colegio-demo")

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.admin_preview.v1")
        self.assertEqual(payload.get("sector"), "educacion")
        self.assertEqual(payload.get("tenant_slug"), "colegio-demo")
        self.assertTrue(payload.get("modules"))
        self.assertTrue(payload.get("cards"))
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "demo_admin_preview")
        self.assertTrue((payload.get("catalog") or {}).get("download_endpoint"))

    def test_v2_demo_catalog_asset_alias_serves_pdf(self):
        resp = self.client.get("/api/v2/demo/catalog-assets/colegio-demo.pdf")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/pdf", resp.headers.get("Content-Type", ""))

    def test_v2_demo_session_accepts_sector_only_for_guided_pillar_start(self):
        owner = User(name="Colegio Demo", email="colegio-sector@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="colegio-sector",
            nombre="Colegio Sector Demo",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
            vertical="educacion",
            subvertical="colegio_privado",
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post("/api/v2/demo/session", json={"sector": "colegios"})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        selector = workspace.get("pillar_selector") or {}
        self.assertEqual(selector.get("contract_version"), "demo.pillars.v1")
        self.assertEqual(selector.get("selected_sector"), "educacion")
        self.assertEqual(selector.get("selected_rubro"), "colegios")
        self.assertTrue(workspace.get("catalog_resources"))
        self.assertEqual((payload.get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")

    def test_v2_demo_session_accepts_rubro_slug_alias(self):
        owner = User(name="Alias Demo", email="alias-demo@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="ferreteria", nombre="Ferreteria Demo", tipo="pyme", pyme_id=owner.id, is_active=True)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post("/api/v2/demo/session", json={"sector": "empresas", "rubro_slug": "ferreteria"})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("tenant_slug"), "ferreteria")
        self.assertEqual((payload.get("chat_bootstrap") or {}).get("payload", {}).get("rubro"), "ferreteria")

    def test_v2_demo_session_from_rubro_returns_matching_chat_bootstrap(self):
        owner = User(name="Demo Rubro", email="demo-rubro@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="bodega", nombre="Bodega Demo", tipo="pyme", pyme_id=owner.id, is_active=True)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "rubro": tenant.slug},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        chat_bootstrap = payload.get("chat_bootstrap") or {}
        self.assertEqual(payload.get("tenant_slug"), tenant.slug)
        self.assertEqual((payload.get("tenant") or {}).get("tipo"), "pyme")
        self.assertEqual(chat_bootstrap.get("endpoint"), "/ask/pyme")
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("rubro"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("rubro_clave"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("context") or {}).get("sector"), "empresas")
        self.assertEqual((chat_bootstrap.get("start_event") or {}).get("type"), "demo_chat_start")
        self.assertEqual((chat_bootstrap.get("start_event") or {}).get("tenant_slug"), tenant.slug)

    def test_v2_demo_session_supports_education_vertical_without_new_chat_app(self):
        owner = User(name="Colegio Demo", email="colegio-demo@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="colegio-demo",
            nombre="Colegio Demo",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
            vertical="educacion",
            subvertical="colegio_privado",
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "educacion", "tenant_slug": tenant.slug},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        chat_bootstrap = payload.get("chat_bootstrap") or {}
        workspace = payload.get("workspace") or {}
        self.assertEqual(chat_bootstrap.get("endpoint"), "/ask/pyme")
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("vertical"), "educacion")
        self.assertEqual((chat_bootstrap.get("context") or {}).get("vertical"), "educacion")
        self.assertEqual((payload.get("experience_blueprint") or {}).get("experience_type"), "education")
        self.assertTrue((workspace.get("education") or {}).get("whatsapp_playbook"))
        self.assertTrue(any("inasistencia" in (item.get("label") or "").lower() for item in workspace.get("quick_replies") or []))

    def test_v2_demo_session_accepts_spanish_aliases_from_frontend(self):
        owner = User(name="Colegio Alias", email="colegio-alias@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="colegio-demo",
            nombre="Colegio Demo",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
            vertical="educacion",
            subvertical="colegio_privado",
            capabilities_json={"education": {"enabled": True}},
        )
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "sector": "educacion",
                "pilar": "colegios",
                "categoria": "colegio-demo",
                "rubro": "colegio-demo",
            },
            headers={"X-Request-Id": "demo-aliases-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("tenant_slug"), "colegio-demo")
        self.assertEqual(payload.get("request_id"), "demo-aliases-1")
        self.assertEqual((payload.get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
        self.assertEqual((payload.get("chat_bootstrap") or {}).get("payload", {}).get("rubro"), "colegio-demo")

    def test_v2_demo_session_accepts_widget_onboarding_payloads_for_three_pillars(self):
        pyme_owner = User(name="Bodega Demo", email="bodega-widget@test.com", password_hash="hash", tipo_chat="pyme")
        colegio_owner = User(name="Colegio Widget", email="colegio-widget@test.com", password_hash="hash", tipo_chat="pyme")
        municipio_owner = User(name="Municipio Widget", email="municipio-widget@test.com", password_hash="hash", tipo_chat="municipio")
        db.session.add_all([pyme_owner, colegio_owner, municipio_owner])
        db.session.flush()
        db.session.add_all(
            [
                TenantProfile(slug="bodega", nombre="Bodega Demo", tipo="pyme", pyme_id=pyme_owner.id, is_active=True),
                TenantProfile(
                    slug="colegio-demo",
                    nombre="Colegio Demo",
                    tipo="pyme",
                    pyme_id=colegio_owner.id,
                    is_active=True,
                    vertical="educacion",
                    subvertical="colegio_privado",
                    capabilities_json={"education": {"enabled": True}},
                ),
                TenantProfile(slug="municipio", nombre="Municipio Demo", tipo="municipio", municipio_id=municipio_owner.id, is_active=True),
            ]
        )
        db.session.commit()

        cases = [
            ("educacion", "colegio-demo", "colegio-demo", "/ask/pyme"),
            ("gobierno", "municipio", "municipio", "/ask/municipio"),
            ("empresas", "bodega", "bodega", "/ask/pyme"),
        ]
        for sector, tenant_slug, rubro, endpoint in cases:
            with self.subTest(sector=sector):
                resp = self.client.post(
                    "/api/v2/demo/session",
                    json={"sector": sector, "tenant_slug": tenant_slug, "rubro": rubro},
                    headers={"X-Request-Id": f"widget-onboarding-{sector}"},
                )

                self.assertEqual(resp.status_code, 200)
                payload = resp.get_json()
                workspace = payload.get("workspace") or {}
                chat_bootstrap = workspace.get("chat_bootstrap") or {}
                headers = chat_bootstrap.get("headers") or {}
                self.assertEqual(payload.get("request_id"), f"widget-onboarding-{sector}")
                self.assertEqual(payload.get("tenant_slug"), tenant_slug)
                self.assertEqual((payload.get("tenant") or {}).get("slug"), tenant_slug)
                self.assertEqual(chat_bootstrap.get("endpoint"), endpoint)
                self.assertEqual(headers.get("X-Demo-Session-Id"), payload.get("demo_session_id"))
                self.assertEqual(headers.get("X-Chat-Session-Id"), payload.get("demo_session_id"))
                self.assertEqual(headers.get("X-Tenant-Slug"), tenant_slug)
                self.assertEqual((chat_bootstrap.get("payload") or {}).get("tenant_slug"), tenant_slug)
                self.assertEqual((chat_bootstrap.get("payload") or {}).get("rubro"), rubro)
                self.assertTrue(workspace.get("media_capabilities"))
                self.assertTrue(workspace.get("conversion_ctas"))
                self.assertTrue(workspace.get("animation_tokens"))
                self.assertTrue(workspace.get("quick_replies") or ((workspace.get("education") or {}).get("quick_menu")))

    def test_api_ask_municipio_alias_degrades_runtime_errors_for_cached_frontend(self):
        owner = User(name="Municipio Demo", email="municipio-alias@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Demo", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        with patch("services.logic.responder_chatboc", side_effect=RuntimeError("runtime boom")):
            resp = self.client.post(
                "/api/ask/municipio?tenant_slug=municipio",
                json={"pregunta": "hola", "demo_mode": True, "tenant_slug": "municipio"},
                headers={
                    "Origin": "https://www.chatboc.ar",
                    "X-Chat-Session-Id": "demo-runtime-alias-1",
                    "X-Request-Id": "runtime-alias-1",
                },
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "chat.runtime_fallback.v1")
        self.assertEqual(payload.get("request_id"), "runtime-alias-1")
        self.assertTrue(payload.get("respuesta_usuario"))
        self.assertIsInstance(payload.get("botones"), list)

    def test_demo_chat_alias_returns_chat_response_contract_with_lead_shape(self):
        owner = User(name="Colegio Demo", email="colegio-chat-contract@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        db.session.add(
            TenantProfile(
                slug="colegio-demo",
                nombre="Colegio Demo",
                tipo="pyme",
                pyme_id=owner.id,
                is_active=True,
                vertical="educacion",
            )
        )
        db.session.commit()

        backend_payload = {
            "message_body": "Perfecto, dejo el caso preparado para secretaria.",
            "botones": [{"texto": "Ver seguimiento", "action_id": "tracking"}],
            "ticket_id": 456,
        }
        with patch("services.logic.responder_chatboc", return_value=backend_payload):
            resp = self.client.post(
                "/api/ask/pyme?tenant_slug=colegio-demo",
                json={
                    "pregunta": "Necesito consultar admisiones",
                    "demo_mode": True,
                    "tenant_slug": "colegio-demo",
                    "rubro": "colegios",
                },
                headers={
                    "Origin": "https://www.chatboc.ar",
                    "X-Chat-Session-Id": "demo-chat-contract-1",
                    "X-Demo-Session-Id": "demo-chat-contract-1",
                    "X-Tenant-Slug": "colegio-demo",
                    "X-Request-Id": "chat-contract-1",
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Request-Id"), "chat-contract-1")
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "chat.response.v1")
        self.assertEqual(payload.get("request_id"), "chat-contract-1")
        self.assertEqual(payload.get("conversation_id"), "demo-chat-contract-1")
        self.assertEqual(payload.get("message"), "Perfecto, dejo el caso preparado para secretaria.")
        self.assertIsInstance(payload.get("messages"), list)
        self.assertEqual((payload.get("lead") or {}).get("ticket_id"), 456)
        self.assertEqual((payload.get("lead") or {}).get("detail_endpoint"), "/api/v2/inbox/omnichannel/456")
        self.assertTrue((payload.get("runtime_contract") or {}).get("server_side_ai"))
        self.assertFalse((payload.get("runtime_contract") or {}).get("frontend_llm_keys_allowed"))

    def test_api_upload_chat_attachment_alias_has_preflight(self):
        resp = self.client.open("/api/archivos/upload/chat_attachment", method="OPTIONS")

        self.assertIn(resp.status_code, {200, 204})

    def test_v2_tenant_route_requires_explicit_tenant_context(self):
        resp = self.client.get("/api/v2/tenants/current")
        self.assertEqual(resp.status_code, 400)

    def test_v2_login_invalid_credentials_returns_401(self):
        resp = self.client.post(
            "/api/v2/auth/login",
            json={"email": "nadie@example.com", "password": "incorrecta"},
        )
        self.assertEqual(resp.status_code, 401)

    def test_v2_refresh_invalid_token_returns_401(self):
        resp = self.client.post("/api/v2/auth/refresh", json={"token": "invalid-token"})
        self.assertEqual(resp.status_code, 401)

    def test_prod_with_default_secret_fails_fast(self):
        with self.assertRaises(RuntimeError):
            create_app(BadProdConfig)


if __name__ == "__main__":
    unittest.main()
