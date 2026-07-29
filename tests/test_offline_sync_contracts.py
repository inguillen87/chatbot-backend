import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import EncRespuesta, MunicipioTicket, PymeTicket


class OfflineSyncTestConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class OfflineSyncContractsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(OfflineSyncTestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_non_durable_sync_endpoints_are_retired_fail_closed(self):
        cases = (
            {
                "path": "/api/surveys/sync",
                "payload": {"responses": [{"id": "resp-1"}]},
                "request_id": "sync-survey-1",
                "canonical_endpoint": "/api/v2/public/surveys/{public_token}/respond",
            },
            {
                "path": "/api/tickets/draft/sync",
                "payload": {"title": "Borrador ticket"},
                "request_id": "sync-ticket-1",
                "canonical_endpoint": "/api/pwa/app/tickets",
            },
        )

        for case in cases:
            with self.subTest(path=case["path"]):
                response = self.client.post(
                    case["path"],
                    json=case["payload"],
                    headers={
                        "X-Request-Id": case["request_id"],
                        "Idempotency-Key": f"key-{case['request_id']}",
                    },
                )

                self.assertEqual(response.status_code, 410)
                payload = response.get_json()
                self.assertEqual(
                    payload,
                    {
                        "action_hint": "use_canonical_endpoint",
                        "canonical_endpoint": case["canonical_endpoint"],
                        "contract_version": "offline.sync.retired.v1",
                        "ok": False,
                        "persisted": False,
                        "reason_code": "non_durable_sync_endpoint",
                        "request_id": case["request_id"],
                        "retryable": False,
                    },
                )
                self.assertEqual(response.headers.get("X-Request-Id"), case["request_id"])

        self.assertEqual(EncRespuesta.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(PymeTicket.query.count(), 0)


if __name__ == "__main__":
    unittest.main()
