import unittest

from flask import Flask

from routes.api_aliases import api_aliases_bp
from routes.auth import AUTH_DEMO_CONTRACT_VERSION, auth_bp


class ApiAliasesDemoContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(auth_bp)
        self.app.register_blueprint(api_aliases_bp)
        self.client = self.app.test_client()

    def test_api_demo_catalog_alias_uses_same_404_contract(self):
        response = self.client.get("/api/auth/demo/catalog")
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], AUTH_DEMO_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_api_demo_catalog_alias_preserves_request_id_header(self):
        response = self.client.get("/api/auth/demo/catalog", headers={"X-Request-Id": "req-123"})
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-123")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-123")

    def test_api_demo_catalog_alias_ignores_blank_request_id_header(self):
        response = self.client.get("/api/auth/demo/catalog", headers={"X-Request-Id": "   "})
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertTrue(body["request_id"])
        self.assertNotEqual(body["request_id"], "   ")
        self.assertEqual(body["request_id"], response.headers.get("X-Request-Id"))

    def test_api_demo_login_alias_uses_same_404_contract(self):
        response = self.client.post("/api/auth/demo", json={"rubro": "pyme"})
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], AUTH_DEMO_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_api_demo_login_alias_preserves_request_id_header(self):
        response = self.client.post(
            "/api/auth/demo",
            json={"rubro": "pyme"},
            headers={"X-Request-Id": "req-login-1"},
        )
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-login-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-login-1")

    def test_api_demo_login_alias_ignores_blank_request_id_header(self):
        response = self.client.post(
            "/api/auth/demo",
            json={"rubro": "pyme"},
            headers={"X-Request-Id": ""},
        )
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertTrue(body["request_id"])
        self.assertEqual(body["request_id"], response.headers.get("X-Request-Id"))


if __name__ == "__main__":
    unittest.main()
