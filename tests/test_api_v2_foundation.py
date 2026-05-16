import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import ChatSessionContext, MunicipioTicket, TenantProfile, User, WhatsappNumero


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
        first_rubro = (payload.get("rubros") or [])[0]
        self.assertEqual((first_rubro.get("sales_story") or {}).get("contract_version"), "demo.sales_story.v1")
        self.assertEqual((first_rubro.get("consulting_playbook") or {}).get("contract_version"), "demo.consulting_playbook.v1")
        self.assertTrue(first_rubro.get("wow_flows"))
        self.assertTrue(first_rubro.get("live_modules"))
        self.assertEqual((first_rubro.get("openai_runtime") or {}).get("provider"), "openai_server_side")
        self.assertFalse((first_rubro.get("openai_runtime") or {}).get("frontend_api_keys_allowed"))

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
        self.assertIn("demo.session.v1", payload.get("contract_aliases") or [])
        self.assertEqual(payload.get("tenant_slug"), tenant.slug)
        self.assertEqual((payload.get("tenant") or {}).get("sector"), "empresas")
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
        self.assertIsNone(chat_bootstrap.get("fallback_endpoint"))
        self.assertEqual(chat_bootstrap.get("method"), "POST")
        self.assertEqual(chat_bootstrap.get("response_contract"), "chat.response.v1")
        self.assertEqual((chat_bootstrap.get("session") or {}).get("chat_session_id"), payload.get("session_id"))
        self.assertEqual((chat_bootstrap.get("session") or {}).get("demo_session_id"), payload.get("demo_session_id"))
        self.assertEqual((chat_bootstrap.get("empty_states") or {}).get("runtime_unavailable", {}).get("title"), "Demo conversacional no disponible")
        self.assertTrue((chat_bootstrap.get("runtime_contract") or {}).get("server_side_ai"))
        self.assertFalse((chat_bootstrap.get("runtime_contract") or {}).get("frontend_llm_keys_allowed"))
        self.assertEqual((chat_bootstrap.get("headers") or {}).get("X-Chat-Session-Id"), payload.get("session_id"))
        self.assertEqual((chat_bootstrap.get("headers") or {}).get("X-Demo-Session-Id"), payload.get("demo_session_id"))
        self.assertLessEqual(len(payload.get("session_id") or ""), 36)
        self.assertEqual((chat_bootstrap.get("headers") or {}).get("X-Tenant-Slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("query") or {}).get("tenant_slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("tipo_chat"), "pyme")
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("tenant_slug"), tenant.slug)
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("demo_mode"), True)
        self.assertEqual((workspace.get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
        self.assertEqual((workspace.get("empty_states") or {}).get("runtime_unavailable", {}).get("title"), "Demo conversacional no disponible")
        self.assertEqual(((payload.get("chat_seed") or {}).get("chat_bootstrap") or {}).get("endpoint"), "/ask/pyme")
        self.assertTrue((chat_bootstrap.get("supports") or {}).get("audio"))
        self.assertTrue((chat_bootstrap.get("supports") or {}).get("image"))
        self.assertEqual((workspace.get("runtime_contract") or {}).get("chat_response_contract"), "chat.response.v1")
        self.assertFalse((workspace.get("runtime_contract") or {}).get("local_mock_allowed"))
        self.assertEqual((workspace.get("sales_story") or {}).get("contract_version"), "demo.sales_story.v1")
        self.assertEqual((workspace.get("consulting_playbook") or {}).get("contract_version"), "demo.consulting_playbook.v1")
        self.assertTrue(workspace.get("wow_flows"))
        self.assertTrue(any(module.get("id") == "inbox" and module.get("enabled") for module in workspace.get("live_modules") or []))
        self.assertEqual((workspace.get("openai_runtime") or {}).get("response_contract"), "chat.response.v1")
        self.assertFalse((workspace.get("openai_runtime") or {}).get("frontend_api_keys_allowed"))
        self.assertIn("lead_capture", (workspace.get("openai_runtime") or {}).get("actionable_intents") or [])
        lead_capture = workspace.get("lead_capture") or {}
        self.assertEqual(lead_capture.get("endpoint"), "/api/public/lead-capture")
        self.assertTrue(lead_capture.get("fields"))
        self.assertEqual(lead_capture.get("required_fields"), ["name"])
        self.assertEqual(lead_capture.get("required_any_of"), [["phone", "email"]])
        self.assertEqual((workspace.get("survey_voting") or {}).get("contract_version"), "demo.survey_voting.v1")
        self.assertIn("/api/public/encuestas/v1/", (workspace.get("survey_voting") or {}).get("respond_endpoint") or "")
        self.assertIn("/api/public/encuestas/v1/", (workspace.get("survey_voting") or {}).get("results_endpoint") or "")
        self.assertIn("/api/public/encuestas/v1/", (workspace.get("survey_voting") or {}).get("comments_endpoint") or "")
        self.assertTrue(workspace.get("allowed_actions"))
        self.assertEqual((workspace.get("tracking") or {}).get("contract_version"), "demo.tracking.v1")
        self.assertIn("/api/v2/demo/admin-preview", workspace.get("admin_preview_endpoint") or "")
        whatsapp_sandbox = workspace.get("whatsapp_sandbox") or {}
        self.assertEqual(whatsapp_sandbox.get("contract_version"), "demo.whatsapp_sandbox.v1")
        self.assertEqual((whatsapp_sandbox.get("trial_policy") or {}).get("max_messages"), 10)
        self.assertTrue((whatsapp_sandbox.get("supported_inputs") or {}).get("image"))
        self.assertTrue((whatsapp_sandbox.get("supported_inputs") or {}).get("audio"))
        self.assertTrue((whatsapp_sandbox.get("supported_inputs") or {}).get("location"))
        self.assertTrue((whatsapp_sandbox.get("supported_inputs") or {}).get("file"))
        self.assertTrue(whatsapp_sandbox.get("scenario_scripts"))
        self.assertEqual((payload.get("whatsapp_sandbox") or {}).get("contract_version"), "demo.whatsapp_sandbox.v1")
        self.assertEqual(((payload.get("chat_seed") or {}).get("whatsapp_sandbox") or {}).get("contract_version"), "demo.whatsapp_sandbox.v1")

    def test_public_whatsapp_sandbox_launcher_requires_no_auth_and_exposes_trial_contract(self):
        owner = User(name="Bodega Demo", email="bodega-sandbox@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="bodega",
            nombre="Bodega Demo",
            tipo="pyme",
            pyme_id=owner.id,
            vertical="empresas",
            subvertical="bodega",
        )
        db.session.add(tenant)
        db.session.add(WhatsappNumero(numero_whatsapp="+18564858589", user_id=owner.id, is_active=True))
        db.session.commit()

        options = self.client.open(
            "/api/v2/demo/whatsapp-sandbox",
            method="OPTIONS",
            headers={"Origin": "https://www.chatboc.ar"},
        )
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.headers.get("Access-Control-Allow-Origin"), "https://www.chatboc.ar")

        resp = self.client.post(
            "/api/v2/demo/whatsapp-sandbox",
            json={"sector": "empresas", "rubro": "bodega", "source": "public_demo_profile"},
            headers={"X-Request-Id": "sandbox-public-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        contract = payload.get("whatsapp_sandbox") or {}
        self.assertEqual(payload.get("contract_version"), "demo.whatsapp_sandbox_launcher.v1")
        self.assertFalse(payload.get("requires_auth"))
        self.assertEqual(payload.get("request_id"), "sandbox-public-1")
        self.assertEqual((payload.get("session") or {}).get("max_messages"), 10)
        self.assertLessEqual(len((payload.get("session") or {}).get("chat_session_id") or ""), 36)
        self.assertNotEqual((payload.get("session") or {}).get("demo_session_id"), (payload.get("session") or {}).get("chat_session_id"))
        self.assertEqual(contract.get("contract_version"), "demo.whatsapp_sandbox.v1")
        self.assertEqual(contract.get("provider"), "twilio_whatsapp_number")
        self.assertEqual((contract.get("sandbox") or {}).get("display_number"), "+18564858589")
        self.assertIn("wa.me/18564858589", (contract.get("sandbox") or {}).get("wa_deeplink") or "")
        self.assertFalse((contract.get("sandbox") or {}).get("requires_join_phrase"))
        self.assertIn("probar la demo", (contract.get("sandbox") or {}).get("activation_message") or "")
        self.assertEqual((contract.get("trial_policy") or {}).get("max_messages"), 10)
        self.assertTrue((contract.get("supported_inputs") or {}).get("image"))
        self.assertTrue((contract.get("supported_inputs") or {}).get("audio"))
        self.assertTrue((contract.get("supported_inputs") or {}).get("location"))
        self.assertTrue((contract.get("supported_inputs") or {}).get("file"))
        self.assertTrue(contract.get("scenario_scripts"))
        self.assertTrue((contract.get("catalog") or {}).get("enabled"))
        self.assertTrue((contract.get("catalog") or {}).get("resources"))
        self.assertFalse((payload.get("frontend_contract") or {}).get("requires_auth"))

    def test_demo_session_returns_short_chat_session_id(self):
        owner = User(name="Demo Short Session", email="demo-short-session@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="demo-short-session", nombre="Demo Short Session", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "tenant_slug": tenant.slug},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertTrue(payload.get("demo_session_id"))
        self.assertTrue(payload.get("chat_session_id"))
        self.assertEqual(payload.get("session_id"), payload.get("chat_session_id"))
        self.assertLessEqual(len(payload.get("chat_session_id") or ""), 36)
        self.assertEqual((payload.get("workspace") or {}).get("chat_bootstrap", {}).get("headers", {}).get("X-Chat-Session-Id"), payload.get("chat_session_id"))

    def test_demo_session_jwt_not_used_as_chat_session_id(self):
        owner = User(name="Demo JWT Session", email="demo-jwt-session@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug="demo-jwt-session", nombre="Demo JWT Session", tipo="pyme", pyme_id=owner.id)
        db.session.add(tenant)
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "tenant_slug": tenant.slug},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertNotEqual(payload.get("demo_session_id"), payload.get("chat_session_id"))
        self.assertLessEqual(len(payload.get("chat_session_id") or ""), 36)

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
        self.assertEqual(payload.get("metrics"), [])
        self.assertEqual((payload.get("map") or {}).get("enabled"), False)
        self.assertEqual((payload.get("map") or {}).get("points"), [])
        self.assertEqual((payload.get("session_activity") or {}).get("has_session_data"), False)
        self.assertEqual((payload.get("operations") or {}).get("data_policy"), "session_events_only")
        self.assertEqual((payload.get("sales_story") or {}).get("contract_version"), "demo.sales_story.v1")
        self.assertTrue(payload.get("wow_flows"))
        self.assertTrue(payload.get("live_modules"))
        self.assertEqual((payload.get("openai_runtime") or {}).get("provider"), "openai_server_side")
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "demo_admin_preview")
        self.assertTrue((payload.get("catalog") or {}).get("download_endpoint"))

    def test_demo_admin_preview_has_no_fake_metrics(self):
        resp = self.client.get("/api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio")

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "demo.admin_preview.v1")
        self.assertEqual(payload.get("metrics"), [])
        self.assertEqual((payload.get("map") or {}).get("enabled"), False)
        self.assertEqual((payload.get("map") or {}).get("points"), [])

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
                session = payload.get("session") or {}
                default_menu = workspace.get("default_menu") or {}
                self.assertEqual(payload.get("request_id"), f"widget-onboarding-{sector}")
                self.assertTrue(payload.get("ok"))
                self.assertTrue(payload.get("ready"))
                self.assertEqual(payload.get("status"), "ready")
                self.assertEqual(payload.get("tenant_slug"), tenant_slug)
                self.assertEqual((payload.get("tenant") or {}).get("slug"), tenant_slug)
                self.assertEqual(session.get("demo_session_id"), payload.get("demo_session_id"))
                self.assertEqual(session.get("chat_session_id"), payload.get("session_id"))
                self.assertEqual(chat_bootstrap.get("endpoint"), endpoint)
                self.assertEqual(headers.get("X-Demo-Session-Id"), payload.get("demo_session_id"))
                self.assertEqual(headers.get("X-Chat-Session-Id"), payload.get("session_id"))
                self.assertLessEqual(len(payload.get("session_id") or ""), 36)
                self.assertEqual(headers.get("X-Tenant-Slug"), tenant_slug)
                self.assertEqual((chat_bootstrap.get("payload") or {}).get("tenant_slug"), tenant_slug)
                self.assertEqual((chat_bootstrap.get("payload") or {}).get("rubro"), rubro)
                self.assertEqual((payload.get("widget_onboarding") or {}).get("status"), "ready")
                self.assertTrue((payload.get("widget_onboarding") or {}).get("open_chat"))
                self.assertEqual((payload.get("frontend_contract") or {}).get("success_condition"), "http_200_and_ok_true")
                self.assertEqual(default_menu.get("contract_version"), "demo.default_menu.v1")
                self.assertTrue(default_menu.get("items"))
                self.assertEqual((chat_bootstrap.get("default_menu") or {}).get("items"), default_menu.get("items"))
                self.assertTrue(workspace.get("media_capabilities"))
                self.assertTrue(workspace.get("conversion_ctas"))
                self.assertTrue(workspace.get("animation_tokens"))
                self.assertTrue(workspace.get("quick_replies") or ((workspace.get("education") or {}).get("quick_menu")))

    def test_v2_demo_session_widget_selector_accepts_label_and_returns_compact_success(self):
        owner = User(name="Municipio Widget", email="municipio-label-widget@test.com", password_hash="hash", tipo_chat="municipio")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Demo", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "label": "Gobiernos",
                "source": "landing_widget_selector",
                "surface": "widget",
            },
            headers={"X-Request-Id": "widget-selector-label-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        session = payload.get("session") or {}
        chat_bootstrap = workspace.get("chat_bootstrap") or {}
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("status"), "ready")
        self.assertEqual(payload.get("response_profile"), "widget_compact")
        self.assertLess(len(resp.get_data()), 90000)
        self.assertEqual(payload.get("request_id"), "widget-selector-label-1")
        self.assertEqual((payload.get("tenant") or {}).get("sector"), "gobierno")
        self.assertEqual(chat_bootstrap.get("endpoint"), "/ask/municipio")
        self.assertEqual(session.get("chat_session_id"), payload.get("chat_session_id"))
        self.assertEqual(session.get("demo_session_id"), payload.get("demo_session_id"))
        self.assertEqual((payload.get("widget_onboarding") or {}).get("status"), "ready")
        self.assertTrue((payload.get("widget_onboarding") or {}).get("close_selector"))
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "demo_session_ready")

    def test_v2_demo_session_widget_selector_inferrs_education_from_cta_text(self):
        owner = User(name="Colegio Widget", email="colegio-cta-widget@test.com", password_hash="hash", tipo_chat="pyme")
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
                subvertical="colegio_privado",
                capabilities_json={"education": {"enabled": True}},
            )
        )
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "label": "Probar colegio",
                "source": "landing_widget_selector",
                "surface": "widget",
            },
            headers={"X-Request-Id": "widget-selector-colegio-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        chat_bootstrap = workspace.get("chat_bootstrap") or {}
        default_menu = workspace.get("default_menu") or {}
        menu_intents = {item.get("intent") for item in default_menu.get("items") or []}
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("response_profile"), "widget_compact")
        self.assertEqual((payload.get("tenant") or {}).get("sector"), "educacion")
        self.assertEqual(payload.get("tenant_slug"), "colegio-demo")
        self.assertEqual(chat_bootstrap.get("endpoint"), "/ask/pyme")
        self.assertEqual((chat_bootstrap.get("payload") or {}).get("vertical"), "educacion")
        self.assertEqual(default_menu.get("contract_version"), "demo.default_menu.v1")
        self.assertIn("justificar_inasistencia", menu_intents)
        self.assertEqual((payload.get("widget_onboarding") or {}).get("default_menu", {}).get("items"), default_menu.get("items"))

    def test_v2_demo_session_empresas_sector_only_returns_rubro_selector(self):
        bodega_owner = User(name="Bodega Selector", email="bodega-selector@test.com", password_hash="hash", tipo_chat="pyme")
        ferreteria_owner = User(name="Ferreteria Selector", email="ferreteria-selector@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add_all([bodega_owner, ferreteria_owner])
        db.session.flush()
        db.session.add_all(
            [
                TenantProfile(slug="bodega", nombre="Bodega Demo", tipo="pyme", pyme_id=bodega_owner.id, is_active=True),
                TenantProfile(slug="ferreteria", nombre="Ferreteria Demo", tipo="pyme", pyme_id=ferreteria_owner.id, is_active=True),
            ]
        )
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "sector": "empresas",
                "label": "Empresas",
                "source": "landing_widget_selector",
                "surface": "widget",
            },
            headers={"X-Request-Id": "widget-selector-empresas-rubros"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        selector = workspace.get("rubro_selector") or {}
        widget_onboarding = payload.get("widget_onboarding") or {}
        category_slugs = {item.get("slug") for item in selector.get("categories") or []}
        self.assertTrue(payload.get("ok"))
        self.assertTrue(payload.get("requires_rubro_selection"))
        self.assertEqual(payload.get("next_step"), "select_rubro")
        self.assertEqual(widget_onboarding.get("status"), "select_rubro")
        self.assertFalse(widget_onboarding.get("open_chat"))
        self.assertFalse(widget_onboarding.get("send_init_once"))
        self.assertEqual(selector.get("contract_version"), "demo.rubro_selector.v1")
        self.assertIn("bodega", category_slugs)
        self.assertIn("ferreteria", category_slugs)
        self.assertEqual((payload.get("frontend_contract") or {}).get("render_as"), "demo_rubro_selector")

    def test_v2_demo_session_pyme_rubro_profiles_are_distinct(self):
        bodega_owner = User(name="Bodega Profile", email="bodega-profile@test.com", password_hash="hash", tipo_chat="pyme")
        ferreteria_owner = User(name="Ferreteria Profile", email="ferreteria-profile@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add_all([bodega_owner, ferreteria_owner])
        db.session.flush()
        db.session.add_all(
            [
                TenantProfile(slug="bodega", nombre="Bodega Demo", tipo="pyme", pyme_id=bodega_owner.id, is_active=True),
                TenantProfile(slug="ferreteria", nombre="Ferreteria Demo", tipo="pyme", pyme_id=ferreteria_owner.id, is_active=True),
            ]
        )
        db.session.commit()

        responses = {}
        for rubro in ("bodega", "ferreteria"):
            resp = self.client.post(
                "/api/v2/demo/session",
                json={
                    "sector": "empresas",
                    "rubro": rubro,
                    "source": "landing_widget_rubro_selector",
                    "surface": "widget",
                },
            )
            self.assertEqual(resp.status_code, 200)
            responses[rubro] = resp.get_json()

        bodega_workspace = responses["bodega"].get("workspace") or {}
        ferreteria_workspace = responses["ferreteria"].get("workspace") or {}
        bodega_context = bodega_workspace.get("rubro_context") or {}
        ferreteria_context = ferreteria_workspace.get("rubro_context") or {}
        bodega_menu = bodega_workspace.get("default_menu") or {}
        ferreteria_menu = ferreteria_workspace.get("default_menu") or {}
        bodega_intents = {item.get("intent") for item in bodega_menu.get("items") or []}
        ferreteria_intents = {item.get("intent") for item in ferreteria_menu.get("items") or []}

        self.assertEqual(bodega_context.get("slug"), "bodega")
        self.assertEqual(ferreteria_context.get("slug"), "ferreteria")
        self.assertIn("vinos", bodega_context.get("prompt_context", ""))
        self.assertIn("ferreteria", ferreteria_context.get("prompt_context", ""))
        self.assertIn("ver_catalogo_vinos", bodega_intents)
        self.assertIn("calcular_materiales", ferreteria_intents)
        self.assertNotEqual(bodega_menu.get("items"), ferreteria_menu.get("items"))
        self.assertEqual(
            (ferreteria_workspace.get("chat_bootstrap") or {}).get("payload", {}).get("demo_metadata", {}).get("key"),
            "ferreteria",
        )

    def test_v2_demo_session_pyme_rubro_tools_include_catalog_prices_and_faq(self):
        owner = User(name="Ferreteria Tools", email="ferreteria-tools@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="ferreteria", nombre="Ferreteria Demo", tipo="pyme", pyme_id=owner.id, is_active=True))
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "sector": "empresas",
                "rubro": "ferreteria",
                "source": "landing_widget_rubro_selector",
                "surface": "widget",
            },
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        workspace = payload.get("workspace") or {}
        tools = workspace.get("rubro_tools") or {}
        enabled_tools = {item.get("id"): item for item in tools.get("enabled_tools") or []}
        default_menu_kinds = {item.get("kind") for item in (workspace.get("default_menu") or {}).get("items") or []}

        self.assertEqual(tools.get("contract_version"), "demo.rubro_tools.v1")
        self.assertIn("catalog", enabled_tools)
        self.assertIn("price_list", enabled_tools)
        self.assertIn("faq", enabled_tools)
        self.assertTrue(enabled_tools["catalog"].get("items"))
        self.assertTrue(enabled_tools["price_list"].get("items"))
        self.assertTrue(enabled_tools["faq"].get("items"))
        self.assertIn("rubro_tool", default_menu_kinds)
        self.assertEqual(
            ((workspace.get("chat_bootstrap") or {}).get("payload") or {}).get("demo_metadata", {}).get("tool_summary", {}).get("faq_preview"),
            tools.get("faq_preview")[:6],
        )
        self.assertEqual(
            ((workspace.get("chat_bootstrap") or {}).get("payload") or {}).get("rubro_tool_summary", {}).get("faq_preview"),
            tools.get("faq_preview")[:6],
        )

    def test_v2_demo_session_gobierno_rubro_tools_include_google_maps_location(self):
        owner = User(name="Municipio Tools", email="municipio-tools@test.com", password_hash="hash", tipo_chat="municipio")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Demo", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "sector": "gobierno",
                "rubro": "municipio",
                "source": "landing_widget_selector",
                "surface": "widget",
            },
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        tools = ((payload.get("workspace") or {}).get("rubro_tools") or {})
        enabled_tools = {item.get("id"): item for item in tools.get("enabled_tools") or []}
        locations = tools.get("locations") or []

        self.assertEqual(tools.get("contract_version"), "demo.rubro_tools.v1")
        self.assertIn("location", enabled_tools)
        self.assertIn("contact", enabled_tools)
        self.assertTrue(locations)
        self.assertIn("google.com/maps", locations[0].get("maps_url") or "")
        self.assertTrue((tools.get("contact") or {}).get("website"))

    def test_ask_pyme_demo_persists_rubro_metadata_from_chat_bootstrap(self):
        owner = User(name="Ferreteria Runtime", email="ferreteria-runtime@test.com", password_hash="hash", tipo_chat="pyme", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="ferreteria", nombre="Ferreteria Demo", tipo="pyme", pyme_id=owner.id, is_active=True))
        db.session.commit()

        session_resp = self.client.post(
            "/api/v2/demo/session",
            json={
                "sector": "empresas",
                "rubro": "ferreteria",
                "source": "landing_widget_rubro_selector",
                "surface": "widget",
            },
        )
        self.assertEqual(session_resp.status_code, 200)
        bootstrap = (session_resp.get_json().get("workspace") or {}).get("chat_bootstrap") or {}
        bootstrap_payload = bootstrap.get("payload") or {}

        with patch("services.logic.responder_chatboc", return_value={"message_body": "ok runtime"}):
            resp = self.client.post(
                "/api/ask/pyme?tenant_slug=ferreteria",
                json={**bootstrap_payload, "pregunta": "hola", "chat_bootstrap": bootstrap},
                headers={
                    "Origin": "https://www.chatboc.ar",
                    "X-Chat-Session-Id": (bootstrap.get("headers") or {}).get("X-Chat-Session-Id", "runtime-ferreteria-1"),
                    "X-Demo-Session-Id": (bootstrap.get("headers") or {}).get("X-Demo-Session-Id", ""),
                    "X-Request-Id": "runtime-ferreteria-demo-metadata",
                },
            )

        self.assertEqual(resp.status_code, 200)
        context = ChatSessionContext.query.first()
        self.assertIsNotNone(context)
        context_data = context.context_data or {}
        self.assertEqual((context_data.get("demo_metadata") or {}).get("key"), "ferreteria")
        self.assertIn("ferreteria", (context_data.get("demo_metadata") or {}).get("prompt_context", ""))
        self.assertTrue((context_data.get("demo_metadata") or {}).get("tool_summary"))

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

    def test_demo_ask_municipio_does_not_persist_jwt_as_chat_session_id(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Demo JWT", email="municipio-jwt@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Demo", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        with patch("services.logic.responder_chatboc", side_effect=RuntimeError("runtime boom")):
            resp = self.client.post(
                f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
                json={"pregunta": "hola", "demo_mode": True, "tenant_slug": "municipio"},
                headers={"Origin": "https://www.chatboc.ar", "X-Request-Id": "demo-jwt-chat-1"},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "chat.runtime_fallback.v1")
        contexts = ChatSessionContext.query.all()
        self.assertEqual(len(contexts), 1)
        self.assertLessEqual(len(contexts[0].chat_session_id), 36)
        self.assertNotEqual(contexts[0].chat_session_id, demo_session_id)
        self.assertEqual((contexts[0].context_data or {}).get("demo_session_id"), demo_session_id)

    def test_ask_municipio_demo_session_creates_short_chat_context(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Context", email="municipio-context@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Context", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        with patch("services.logic.responder_chatboc", return_value={"message_body": "Caso recibido.", "ticket_id": 123}):
            resp = self.client.post(
                f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
                json={"pregunta": "hola", "demo_mode": True, "tenant_slug": "municipio"},
                headers={"Origin": "https://www.chatboc.ar", "X-Request-Id": "demo-context-1"},
            )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "chat.response.v1")
        self.assertIn("chat.runtime.v1", payload.get("contract_aliases") or [])
        self.assertEqual((payload.get("session") or {}).get("demo_session_id"), demo_session_id)
        context = ChatSessionContext.query.first()
        self.assertIsNotNone(context)
        self.assertLessEqual(len(context.chat_session_id), 36)
        self.assertEqual((context.context_data or {}).get("demo_session_id"), demo_session_id)

    def test_ask_municipio_demo_session_without_header_reuses_stable_chat_context(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Stable", email="municipio-stable@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Stable", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        captured_session_ids = []

        def fake_responder(*args, **kwargs):
            captured_session_ids.append(kwargs.get("chat_session_uuid"))
            return {"message_body": "Caso recibido."}

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        with patch("services.logic.responder_chatboc", side_effect=fake_responder):
            for question in ("__INIT__", "Quiero iniciar un reclamo por alumbrado publico."):
                resp = self.client.post(
                    f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
                    json={"pregunta": question, "demo_mode": True, "tenant_slug": "municipio"},
                    headers={"Origin": "https://www.chatboc.ar"},
                )
                self.assertEqual(resp.status_code, 200)

        contexts = ChatSessionContext.query.all()
        self.assertEqual(len(contexts), 1)
        self.assertTrue(contexts[0].chat_session_id.startswith("sid_"))
        self.assertLessEqual(len(contexts[0].chat_session_id), 36)
        self.assertNotEqual(contexts[0].chat_session_id, demo_session_id)
        self.assertEqual((contexts[0].context_data or {}).get("demo_session_id"), demo_session_id)
        self.assertEqual(captured_session_ids, [contexts[0].chat_session_id])
        self.assertEqual(MunicipioTicket.query.count(), 1)
        ticket = MunicipioTicket.query.first()
        self.assertEqual(ticket.categoria, "Alumbrado publico")
        self.assertEqual(ticket.canal_ingreso, "web_demo_widget")

    def test_demo_municipio_runtime_creates_claim_for_baches_not_parking(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Baches", email="municipio-baches@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Baches", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        resp = self.client.post(
            f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
            json={
                "pregunta": "Donde reporto baches con ubicacion?",
                "demo_mode": True,
                "tenant_slug": "municipio",
                "location": {"lat": -34.61, "lng": -58.44, "address": "Av. San Martin 123"},
            },
            headers={"Origin": "https://www.chatboc.ar", "X-Request-Id": "demo-baches-1"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        body = (payload.get("message_body") or "").lower()
        self.assertEqual(payload.get("fuente"), "demo_municipio_runtime")
        self.assertIn("reclamo", body)
        self.assertNotIn("estacionamiento", body)
        self.assertEqual((payload.get("ticket") or {}).get("category"), "Baches y calzada")

        ticket = MunicipioTicket.query.first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.categoria, "Baches y calzada")
        self.assertEqual(ticket.latitud, -34.61)
        self.assertEqual(ticket.longitud, -58.44)
        self.assertEqual(ticket.direccion, "Av. San Martin 123")

        chat_session_id = (payload.get("session") or {}).get("chat_session_id")
        preview = self.client.get(
            f"/api/v2/demo/admin-preview?sector=gobierno&tenant_slug=municipio&chat_session_id={chat_session_id}"
        )
        self.assertEqual(preview.status_code, 200)
        preview_payload = preview.get_json()
        self.assertTrue((preview_payload.get("session_activity") or {}).get("has_session_data"))
        self.assertEqual((preview_payload.get("cards") or [])[0].get("value"), "1")
        self.assertTrue((preview_payload.get("map") or {}).get("enabled"))
        self.assertEqual(len((preview_payload.get("map") or {}).get("points") or []), 1)

    def test_demo_municipio_runtime_answers_license_without_generic_menu(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Licencia", email="municipio-licencia@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Licencia", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        resp = self.client.post(
            f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
            json={"pregunta": "Necesito saber como sacar un turno para licencia.", "demo_mode": True, "tenant_slug": "municipio"},
            headers={"Origin": "https://www.chatboc.ar"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        body = payload.get("message_body") or ""
        self.assertEqual(payload.get("fuente"), "demo_municipio_runtime")
        self.assertIn("licencia", body.lower())
        self.assertNotIn("Elegí una opción", body)
        self.assertEqual(MunicipioTicket.query.count(), 0)

    def test_demo_municipio_runtime_accepts_image_attachment_for_claim(self):
        from routes.v2.tenants import create_demo_session_token

        owner = User(name="Municipio Imagen", email="municipio-imagen@test.com", password_hash="hash", tipo_chat="municipio", rol="admin")
        db.session.add(owner)
        db.session.flush()
        db.session.add(TenantProfile(slug="municipio", nombre="Municipio Imagen", tipo="municipio", municipio_id=owner.id, is_active=True))
        db.session.commit()

        demo_session_id = create_demo_session_token(tenant_slug="municipio", sector="gobierno", rubro="gobierno")
        image_url = "https://example.com/luminaria.jpg"
        resp = self.client.post(
            f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={demo_session_id}",
            json={
                "pregunta": "Se rompio una luminaria, mando foto",
                "demo_mode": True,
                "tenant_slug": "municipio",
                "attachmentInfo": {"id": "att-demo-1", "url": image_url, "mimeType": "image/jpeg", "name": "luminaria.jpg"},
            },
            headers={"Origin": "https://www.chatboc.ar"},
        )

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload.get("fuente"), "demo_municipio_runtime")
        self.assertIn("image", ((payload.get("media_understanding") or {}).get("received") or []))
        ticket = MunicipioTicket.query.first()
        self.assertIsNotNone(ticket)
        self.assertEqual(ticket.categoria, "Alumbrado publico")
        self.assertEqual(ticket.foto_url_directa, image_url)

    def test_ask_invalid_demo_session_returns_json_error(self):
        invalid_demo_session_id = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid.signature"

        resp = self.client.post(
            f"/api/ask/municipio?tenant_slug=municipio&demo_session_id={invalid_demo_session_id}",
            json={"pregunta": "hola", "demo_mode": True, "tenant_slug": "municipio"},
            headers={"Origin": "https://www.chatboc.ar", "X-Request-Id": "invalid-demo-session-1"},
        )

        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.headers.get("X-Request-Id"), "invalid-demo-session-1")
        payload = resp.get_json()
        self.assertEqual(payload.get("contract_version"), "shared.error.v1")
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("reason_code"), "demo_session_expired")
        self.assertIn("request_id", payload)

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
