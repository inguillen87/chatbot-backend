import unittest
from app import create_app, db
from config import Config
from models_audit import PromptVersion
from services.prompt_registry import prompt_registry
from schemas.ai_contracts import GatewayRequest
from services.ai_gateway import AIGateway

class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"

class MockProvider:
    def generate(self, req):
        from schemas.ai_contracts import GatewayResponse
        return GatewayResponse(
            request_id=req.request_id,
            model=req.model,
            status="completed",
            text=f"Processed: {req.instructions}"
        )

class TestPromptRegistry(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_prompt_registry_resolution(self):
        # Insert test prompts
        base_prompt = PromptVersion(
            prompt_key="welcome",
            version="1.0",
            model_default="gpt-base",
            instructions="Hello, {name}!",
            status="active"
        )
        tenant_prompt = PromptVersion(
            prompt_key="welcome",
            version="1.1",
            tenant_override=42,
            model_default="gpt-tenant",
            instructions="Hi {name}, from tenant 42!",
            status="active"
        )
        db.session.add(base_prompt)
        db.session.add(tenant_prompt)
        db.session.commit()

        # Gateway resolution global
        gw = AIGateway(provider=MockProvider())
        req = GatewayRequest(
            tenant_id=1,
            channel="web",
            instructions_key="welcome",
            prompt_variables={"name": "Alice"}
        )
        res = gw.execute(req)
        self.assertIn("Hello, Alice!", res.text)
        self.assertEqual(res.model, "gpt-base")

        # Gateway resolution tenant override
        req2 = GatewayRequest(
            tenant_id=42,
            channel="web",
            instructions_key="welcome",
            prompt_variables={"name": "Bob"}
        )
        res2 = gw.execute(req2)
        self.assertIn("Hi Bob, from tenant 42!", res2.text)
        self.assertEqual(res2.model, "gpt-tenant")

if __name__ == "__main__":
    unittest.main()
