import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.public_resolver import TENANT_PROFILE_CONTRACT_VERSION, public_resolver_bp
from services.tenant_resolver import TenantResolutionError


class _FakeTenant:
    def __init__(self):
        self.slug = "colegio-san-martin"
        self.tipo = "pyme"
        self.configuracion = {}
        self.widget_settings = None

    def to_public_dict(self):
        return {"slug": self.slug, "tipo": self.tipo}


class PublicResolverTenantProfileContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(public_resolver_bp)
        self.client = self.app.test_client()

    def test_tenant_profile_success_includes_contract_version(self):
        tenant = _FakeTenant()
        with patch("routes.public_resolver.resolve_tenant_only", return_value=tenant), patch(
            "routes.public_resolver._normalize_widget_config", return_value={"children": []}
        ), patch("routes.public_resolver._marketplace_meta", return_value={}), patch(
            "routes.public_resolver._canonical_widget_token", return_value=None
        ):
            response = self.client.get("/api/public/tenant-profile?tenant=colegio-san-martin")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["contract_version"], TENANT_PROFILE_CONTRACT_VERSION)
        self.assertEqual(body["tenant"]["slug"], "colegio-san-martin")

    def test_tenant_profile_not_found_includes_contract_version(self):
        with patch(
            "routes.public_resolver.resolve_tenant_only",
            side_effect=TenantResolutionError("Tenant slug 'foo' not found"),
        ), patch("routes.public_resolver._try_get_demo_tenant", return_value=None):
            response = self.client.get("/api/public/tenant-profile?tenant=foo")

        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], TENANT_PROFILE_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)


if __name__ == "__main__":
    unittest.main()
