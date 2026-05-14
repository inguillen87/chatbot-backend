import unittest
from unittest.mock import patch

from flask import Flask

from routes.public_resolver import WIDGET_CONFIG_CONTRACT_VERSION, public_municipios_bp, public_resolver_bp
from services.tenant_resolver import TenantResolutionError


class _FakeTenant:
    id = 1
    slug = "colegio-san-martin"
    tipo = "pyme"
    nombre = "Colegio San Martin"
    configuracion = {}
    widget_settings = None

    def to_public_dict(self):
        return {"slug": self.slug, "tipo": self.tipo}


class PublicResolverWidgetConfigContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(public_resolver_bp)
        self.app.register_blueprint(public_municipios_bp)
        self.client = self.app.test_client()

    def test_widget_config_includes_contract_version(self):
        with patch("routes.public_resolver.resolve_tenant_only", return_value=_FakeTenant()), patch(
            "routes.public_resolver._build_widget_embed_payload",
            return_value={
                "builder_config": {"theme": "dark"},
                "support_channels": {"live_chat": {"realtime": False}},
                "realtime": {"socket_enabled": False},
                "visibility_rules": {"allow_websocket": False},
                "onboarding": {"mode": "tenant_quick_menu"},
                "media_capabilities": {"input_modes": {"text": {"enabled": True}}},
                "ui_hints": {"density": "compact"},
            },
        ):
            response = self.client.get("/api/public/widget-config?tenant=colegio-san-martin")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], WIDGET_CONFIG_CONTRACT_VERSION)
        self.assertEqual(body["tenant"]["slug"], "colegio-san-martin")
        self.assertFalse(body["support_channels"]["live_chat"]["realtime"])
        self.assertFalse(body["realtime"]["socket_enabled"])
        self.assertFalse(body["visibility_rules"]["allow_websocket"])
        self.assertEqual(body["onboarding"]["mode"], "tenant_quick_menu")
        self.assertEqual(body["ui_hints"]["density"], "compact")
        self.assertFalse(body["suppress_global_widget"])

    def test_widget_config_without_tenant_returns_platform_selector(self):
        response = self.client.get(
            "/api/public/widget-config",
            headers={"Host": "www.chatboc.ar"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], WIDGET_CONFIG_CONTRACT_VERSION)
        self.assertEqual(body["tenant"]["slug"], "chatboc-platform")
        self.assertEqual(body["tenant"]["tipo"], "platform")
        self.assertEqual(body["onboarding"]["contract_version"], "public.widget_onboarding.v1")
        self.assertEqual(body["onboarding"]["mode"], "platform_sector_selector")
        self.assertEqual(body["onboarding"]["selection_endpoint"], "/api/v2/demo/session")
        self.assertEqual(body["onboarding"]["catalog_endpoint"], "/api/v2/demo/catalog")
        self.assertEqual([item["sector"] for item in body["quick_menu"]], ["educacion", "gobierno", "empresas"])
        self.assertEqual(body["onboarding"]["quick_menu"], body["quick_menu"])
        self.assertLessEqual(len(body["quick_menu"]), body["ui_hints"]["max_visible_quick_replies"])
        for item in body["quick_menu"]:
            self.assertTrue(item["label"])
            self.assertIn(item["sector"], {"educacion", "gobierno", "empresas"})
            self.assertTrue(item["tenant_slug"])
            self.assertTrue(item["rubro"])
        self.assertEqual(body["demo_catalog"]["contract_version"], "demo.pillars.v1")
        self.assertIn("media_capabilities", body)
        self.assertEqual(body["ui_hints"]["contract_version"], "widget.ui_hints.v1")
        self.assertEqual(body["ui_hints"]["density"], "compact")
        self.assertEqual(body["ui_hints"]["max_visible_quick_replies"], 3)
        accessibility = body["ui_hints"]["accessibility"]
        self.assertTrue(accessibility["enabled"])
        self.assertTrue(accessibility["allow_dyslexia_mode"])
        self.assertTrue(accessibility["allow_high_contrast"])
        self.assertTrue(accessibility["allow_large_controls"])
        self.assertTrue(accessibility["captions_enabled"])
        self.assertTrue(accessibility["respect_prefers_reduced_motion"])
        self.assertEqual(accessibility["touch_target_min_px"], 44)
        self.assertFalse(body["realtime"]["socket_enabled"])
        self.assertFalse(body["visibility_rules"]["allow_websocket"])
        self.assertFalse(body["support_channels"]["live_chat"]["realtime"])
        self.assertFalse(body["support_channels"]["live_chat"]["socket_enabled"])
        self.assertFalse(body["suppress_global_widget"])

    def test_widget_config_without_tenant_uses_forwarded_platform_host(self):
        response = self.client.get(
            "/api/public/widget-config",
            headers={
                "Host": "chatbot-backend-2e14.onrender.com",
                "X-Forwarded-Host": "www.chatboc.ar",
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["tenant"]["slug"], "chatboc-platform")
        self.assertEqual(body["onboarding"]["mode"], "platform_sector_selector")
        self.assertFalse(body["realtime"]["socket_enabled"])

    def test_landing_experience_without_tenant_returns_platform_contract(self):
        response = self.client.get("/api/public/landing-experience")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "public.landing_experience.v1")
        self.assertEqual(body["experience_kind"], "platform")
        self.assertEqual(body["hero"]["contract_scope"], "operational_demo_data")
        self.assertEqual(body["hero"]["headline"], "Converti conversaciones en operaciones reales")
        self.assertEqual(body["hero"]["primary_cta"]["href"], "/demo")
        self.assertEqual(body["hero"]["secondary_cta"]["href"], "/contacto")
        self.assertEqual(body["hero"]["conversation_demo"]["contract_version"], "landing.hero_conversation_demo.v1")
        self.assertEqual(body["conversion"]["journey"]["contract_version"], "public.conversion_journey.v1")
        self.assertTrue(body["conversion"]["journey"]["frontend_rules"]["do_not_post_empty_leads"])
        self.assertTrue(body["runtime_rules"]["frontend_owns_visual_design"])
        self.assertTrue(body["runtime_rules"]["backend_owns_visible_copy"])
        self.assertNotIn("brand", body)
        self.assertNotIn("design_tokens", body)

    def test_landing_experience_does_not_publish_frontend_visual_tokens_or_page_sections(self):
        response = self.client.get("/api/public/landing-experience")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertNotIn("navigation", body)
        self.assertNotIn("sections", body)
        self.assertNotIn("proof_bar", body)
        self.assertNotIn("pricing_teaser", body)
        self.assertNotIn("faq", body)
        self.assertNotIn("motion", body)
        self.assertIn("journey", body["conversion"])

    def test_realtime_voice_capabilities_returns_platform_contract(self):
        response = self.client.get("/api/public/realtime/voice-capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "realtime.voice_capabilities.v1")
        self.assertEqual(body["provider"], "openai_realtime")
        self.assertEqual(body["recommended_model"], "gpt-realtime-2")
        self.assertEqual(body["fallback_model"], "gpt-realtime")
        self.assertTrue(body["features"]["semantic_vad"])
        self.assertFalse(body["features"]["server_vad"])
        self.assertTrue(body["support_channels"]["voice_call"]["enabled"])
        self.assertIn("colegio", body["verticals"])
        self.assertIn("request_id", body)

    def test_realtime_voice_capabilities_legacy_public_alias_returns_platform_contract(self):
        response = self.client.open("/public/realtime/voice-capabilities", method="OPTIONS")

        self.assertEqual(response.status_code, 200)

        response = self.client.get("/public/realtime/voice-capabilities")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "realtime.voice_capabilities.v1")

    def test_realtime_voice_capabilities_disabled_tenant_returns_degradable_200(self):
        with patch("routes.public_resolver.resolve_tenant_only", return_value=_FakeTenant()), patch(
            "routes.public_resolver._normalize_widget_config",
            return_value={"realtime_voice_enabled": False},
        ):
            response = self.client.get("/api/public/realtime/voice-capabilities?tenant=colegio-san-martin")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "realtime.voice_capabilities.v1")
        self.assertFalse(body["enabled"])
        self.assertEqual(body["reason_code"], "voice_not_enabled")
        self.assertFalse(body["features"]["tool_calling"])
        self.assertFalse(body["support_channels"]["voice_call"]["enabled"])

    def test_landing_experience_with_tenant_uses_resolver(self):
        with patch("routes.public_resolver.resolve_tenant_only", return_value=_FakeTenant()):
            response = self.client.get("/api/public/landing-experience?tenant=colegio-san-martin&page=colegios")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "public.landing_experience.v1")
        self.assertEqual(body["tenant"]["slug"], "colegio-san-martin")
        self.assertTrue(body["tenant"]["white_label"])
        self.assertEqual(body["selected_page"], "colegios")

    def test_widget_config_not_found_uses_standard_error_contract(self):
        with patch(
            "routes.public_resolver.resolve_tenant_only",
            side_effect=TenantResolutionError("Tenant no encontrado"),
        ), patch("routes.public_resolver._try_get_demo_tenant", return_value=None):
            response = self.client.get("/api/public/widget-config?tenant=inexistente")

        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], WIDGET_CONFIG_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)


if __name__ == "__main__":
    unittest.main()
