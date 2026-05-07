import unittest
from unittest.mock import patch

from flask import Flask

from routes.public_resolver import WIDGET_CONFIG_CONTRACT_VERSION, public_resolver_bp
from services.tenant_resolver import TenantResolutionError


class _FakeTenant:
    slug = "colegio-san-martin"
    tipo = "pyme"

    def to_public_dict(self):
        return {"slug": self.slug, "tipo": self.tipo}


class PublicResolverWidgetConfigContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(public_resolver_bp)
        self.client = self.app.test_client()

    def test_widget_config_includes_contract_version(self):
        with patch("routes.public_resolver.resolve_tenant_only", return_value=_FakeTenant()), patch(
            "routes.public_resolver._build_widget_embed_payload",
            return_value={"builder_config": {"theme": "dark"}},
        ):
            response = self.client.get("/api/public/widget-config?tenant=colegio-san-martin")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], WIDGET_CONFIG_CONTRACT_VERSION)
        self.assertEqual(body["tenant"]["slug"], "colegio-san-martin")

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
        self.assertEqual(body["recommended_model"], "gpt-realtime-2")
        self.assertIn("colegio", body["verticals"])

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
