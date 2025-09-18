import unittest
from unittest.mock import patch

from flask import Flask, current_app, g

from utils.auth_helpers import get_or_create_anon_id


class GetOrCreateAnonIdTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            ANON_SESSION_COOKIE_NAME="test_anon_cookie",
        )

    def test_reuses_cookie_value_when_present(self):
        cookie_value = "existing-anon-id"
        cookie_header = {
            "Cookie": f"{self.app.config['ANON_SESSION_COOKIE_NAME']}={cookie_value}"
        }

        with self.app.test_request_context("/", headers=cookie_header):
            with patch("utils.auth_helpers.uuid.uuid4") as mock_uuid, patch.object(
                current_app.logger, "info"
            ) as mock_logger_info:
                anon_id = get_or_create_anon_id()
                ctx_anon_id = getattr(g, "anon_id", None)

        self.assertEqual(anon_id, cookie_value)
        self.assertEqual(ctx_anon_id, cookie_value)
        mock_uuid.assert_not_called()
        mock_logger_info.assert_not_called()

    def test_generates_new_uuid_when_cookie_missing_or_empty(self):
        cookie_header = {"Cookie": f"{self.app.config['ANON_SESSION_COOKIE_NAME']}="}

        with self.app.test_request_context("/", headers=cookie_header):
            with patch(
                "utils.auth_helpers.uuid.uuid4", return_value="generated-anon-id"
            ) as mock_uuid, patch.object(
                current_app.logger, "info"
            ) as mock_logger_info:
                anon_id = get_or_create_anon_id()
                ctx_anon_id = getattr(g, "anon_id", None)

        self.assertEqual(anon_id, "generated-anon-id")
        self.assertEqual(ctx_anon_id, "generated-anon-id")
        mock_uuid.assert_called_once()
        mock_logger_info.assert_called_once()


if __name__ == "__main__":
    unittest.main()
