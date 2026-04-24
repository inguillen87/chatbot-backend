import unittest
from schemas.ai_contracts import GatewayRequest, GatewayInputItem
from services.ai_gateway import AIGateway

class MockProviderStream:
    def generate_stream(self, req):
        yield {"type": "response.delta", "data": {"text": "Stream "}}
        yield {"type": "response.delta", "data": {"text": "works!"}}
        yield {"type": "response.completed", "data": {"status": "completed", "text": "Stream works!"}}

class TestAIGatewayStream(unittest.TestCase):
    def test_execute_stream_orchestration(self):
        gw = AIGateway(provider=MockProviderStream())

        req = GatewayRequest(
            tenant_id=1,
            channel="web",
            model="gpt-mock",
            instructions="mock",
            input_items=[GatewayInputItem(text="hi")]
        )

        events = list(gw.execute_stream(req))
        self.assertTrue(len(events) == 4) # start + 2 deltas + completed
        self.assertTrue("event: response.started" in events[0])
        self.assertTrue("event: response.delta" in events[1])
        self.assertTrue("event: response.completed" in events[3])

if __name__ == "__main__":
    unittest.main()
