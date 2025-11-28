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

    def test_options_request_handled_without_manual_cors(self):
        with self.app.test_client() as client:
            resp = client.options('/protected')
            self.assertEqual(resp.status_code, 204)
            # Verify headers are NOT set by decorator
            self.assertNotIn('Access-Control-Allow-Origin', resp.headers)
            self.assertNotIn('Access-Control-Allow-Methods', resp.headers)
            # But Anon-Id should be set
            self.assertIn('X-Anon-Id', resp.headers)

if __name__ == "__main__":
    unittest.main()
