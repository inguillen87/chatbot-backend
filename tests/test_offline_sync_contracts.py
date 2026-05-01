import os
import unittest

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config


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

    def test_survey_sync_ack_contract(self):
        response = self.client.post(
            "/api/surveys/sync",
            json={"responses": [{"id": "resp-1"}]},
            headers={"X-Request-Id": "sync-survey-1", "Idempotency-Key": "survey-sync-key"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("contract_version"), "surveys.sync.v1")
        self.assertEqual(payload.get("request_id"), "sync-survey-1")
        self.assertEqual(payload.get("synced"), 1)
        self.assertEqual(payload.get("idempotency_key"), "survey-sync-key")

    def test_ticket_draft_sync_ack_contract(self):
        response = self.client.post(
            "/api/tickets/draft/sync",
            json={"title": "Borrador ticket"},
            headers={"X-Request-Id": "sync-ticket-1", "Idempotency-Key": "ticket-sync-key"},
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("contract_version"), "tickets.draft_sync.v1")
        self.assertEqual(payload.get("request_id"), "sync-ticket-1")
        self.assertEqual(payload.get("ticket_id"), "TCK-TICKET-SYNC-KEY")
        self.assertEqual(payload.get("idempotency_key"), "ticket-sync-key")


if __name__ == "__main__":
    unittest.main()
