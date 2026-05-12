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
        self.assertEqual(body["onboarding"]["mode"], "platform_sector_selector")
        self.assertEqual([item["sector"] for item in body["quick_menu"]], ["educacion", "gobierno", "empresas"])
        self.assertEqual(body["demo_catalog"]["contract_version"], "demo.pillars.v1")
        self.assertIn("media_capabilities", body)
        self.assertEqual(body["ui_hints"]["density"], "compact")
        self.assertFalse(body["realtime"]["socket_enabled"])
        self.assertFalse(body["visibility_rules"]["allow_websocket"])
        self.assertFalse(body["suppress_global_widget"])

    def test_landing_experience_without_tenant_returns_platform_contract(self):
        response = self.client.get("/api/public/landing-experience")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "public.landing_experience.v1")
        self.assertEqual(body["experience_kind"], "platform")
        self.assertEqual(body["hero"]["h1"], "Chatboc")

    def test_realtime_voice_capabilities_returns_platform_contract(self):
        response = self.client.get("/api/public/realtime/voice-capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], "realtime.voice_capabilities.v1")
        self.assertEqual(body["provider"], "openai_realtime")
        self.assertEqual(body["recommended_model"], "gpt-realtime")
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
