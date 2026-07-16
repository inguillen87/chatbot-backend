import unittest
from flask import Flask, jsonify
from utils.auth_helpers import token_requerido

class AuthCorsTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = 'test'
        self.app.config['ANON_SESSION_COOKIE_NAME'] = 'anon_id'

        # Mock user_from_token to avoid DB/JWT complexity for this specific test
        # Actually token_requerido calls obtener_token -> ... -> user_from_token
        # For OPTIONS, it shouldn't reach user_from_token.

        @self.app.route('/protected', methods=['GET', 'OPTIONS'])
        @token_requerido
        def protected(user):
            return jsonify({"msg": "ok"})

    def test_options_request_exposes_preflight_without_opening_origin(self):
        with self.app.test_client() as client:
            resp = client.options('/protected')
            self.assertEqual(resp.status_code, 204)
            # Without an Origin header the decorator must not grant an origin,
            # while still returning a complete browser preflight contract.
            self.assertNotIn('Access-Control-Allow-Origin', resp.headers)
            self.assertIn('GET', resp.headers.get('Access-Control-Allow-Methods', ''))
            self.assertIn('OPTIONS', resp.headers.get('Access-Control-Allow-Methods', ''))
            self.assertIn('Authorization', resp.headers.get('Access-Control-Allow-Headers', ''))
            # But Anon-Id should be set
            self.assertIn('X-Anon-Id', resp.headers)

if __name__ == "__main__":
    unittest.main()
