import unittest

from flask import Flask

from routes.auth import AUTH_DEMO_CONTRACT_VERSION, auth_bp


class AuthDemoModeContractTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(auth_bp)
        self.client = self.app.test_client()

    def test_demo_catalog_returns_404_contract_when_demo_mode_disabled(self):
        response = self.client.get("/auth/demo/catalog")
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], AUTH_DEMO_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)

    def test_demo_login_returns_404_contract_when_demo_mode_disabled(self):
        response = self.client.post("/auth/demo", json={"rubro": "pyme"})
        self.assertEqual(response.status_code, 404)
        body = response.get_json()
        self.assertEqual(body["contract_version"], AUTH_DEMO_CONTRACT_VERSION)
        self.assertEqual(body["error"]["code"], 404)


if __name__ == "__main__":
    unittest.main()
