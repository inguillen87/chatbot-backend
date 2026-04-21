import unittest

from flask import Flask

from routes.analytics import _json_response


class AnalyticsRequestIdResponseTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_json_response_injects_request_id_when_missing(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            response = _json_response({"ok": True}, status=202)

        self.assertEqual(response.status_code, 202)
        body = response.get_json()
        self.assertTrue(body.get("request_id"))
        self.assertEqual(body["request_id"], response.headers.get("X-Request-Id"))

    def test_json_response_replaces_blank_request_id(self):
        with self.app.test_request_context("/analytics/event", method="POST"):
            response = _json_response({"ok": True, "request_id": "   "}, status=200)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body.get("request_id"))
        self.assertNotEqual(body["request_id"], "   ")
        self.assertEqual(body["request_id"], response.headers.get("X-Request-Id"))

    def test_json_response_respects_forwarded_request_id_header(self):
        with self.app.test_request_context(
            "/analytics/event",
            method="POST",
            headers={"X-Request-Id": "req-forwarded-1"},
        ):
            response = _json_response({"ok": True}, status=200)

        body = response.get_json()
        self.assertEqual(body["request_id"], "req-forwarded-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-forwarded-1")


if __name__ == "__main__":
    unittest.main()
