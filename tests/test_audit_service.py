import unittest
import uuid
from app import create_app, db
from config import Config
from services.audit_service import audit_service
from models_audit import AIRequestLog, AIToolCallLog
from schemas.ai_contracts import GatewayRequest, GatewayResponse, UsageMetrics

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

class TestAuditService(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_log_request_chains_hashes(self):
        req1 = GatewayRequest(tenant_id=1, channel="web", model="gpt-4", instructions="")
        res1 = GatewayResponse(
            request_id=req1.request_id,
            model="gpt-4",
            status="completed",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        )

        log1 = audit_service.log_request(req1, res1)
        self.assertIsNotNone(log1)
        self.assertIsNone(log1.prev_hash)
        self.assertIsNotNone(log1.hash)

        req2 = GatewayRequest(tenant_id=1, channel="web", model="gpt-4", instructions="")
        res2 = GatewayResponse(
            request_id=req2.request_id,
            model="gpt-4",
            status="completed"
        )

        log2 = audit_service.log_request(req2, res2)
        self.assertEqual(log2.prev_hash, log1.hash)
        self.assertIsNotNone(log2.hash)

    def test_log_tool_calls(self):
        req = GatewayRequest(tenant_id=1, channel="web", model="gpt-4", instructions="")
        res = GatewayResponse(request_id=req.request_id, model="gpt-4", status="completed")

        log_entry = audit_service.log_request(req, res)

        tools_data = [{
            "id": "call_abc",
            "name": "get_weather",
            "arguments": '{"location": "Buenos Aires"}',
            "result": '{"temp": 25}',
            "is_error": False
        }]

        audit_service.log_tool_calls(log_entry.id, tools_data)

        tool_logs = AIToolCallLog.query.filter_by(request_log_id=log_entry.id).all()
        self.assertEqual(len(tool_logs), 1)
        self.assertEqual(tool_logs[0].tool_name, "get_weather")
        self.assertEqual(tool_logs[0].is_error, False)

if __name__ == "__main__":
    unittest.main()
