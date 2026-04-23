import unittest
from schemas.ai_contracts import GatewayRequest, GatewayInputItem
from schemas.ai_stream import AIStreamEvent
from services.ai_stream_service import AIStreamService

class MockStreamProvider:
    def generate_stream(self, request: GatewayRequest):
        yield {"type": "response.delta", "data": {"text": "Hello "}}
        yield {"type": "response.delta", "data": {"text": "World"}}
        yield {"type": "response.completed", "data": {"status": "completed", "text": "Hello World"}}

class TestAIStreamService(unittest.TestCase):
    def test_stream_mapping(self):
        provider = MockStreamProvider()
        streamer = AIStreamService()

        req = GatewayRequest(
            tenant_id="123",
            channel="test",
            model="gpt-mock",
            instructions="mock",
            input_items=[GatewayInputItem(text="hi")]
        )

        gen = streamer.stream_response(req, provider.generate_stream(req))
        events = list(gen)

        self.assertTrue(len(events) == 4) # start + 2 delta + end
        self.assertTrue("event: response.started" in events[0])
        self.assertTrue("data: " in events[0])
        self.assertTrue("event: response.delta" in events[1])
        self.assertTrue("Hello" in events[1])
        self.assertTrue("event: response.completed" in events[3])

if __name__ == "__main__":
    unittest.main()
