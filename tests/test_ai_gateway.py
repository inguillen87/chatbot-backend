import unittest
from schemas.ai_contracts import GatewayRequest, GatewayResponse, GatewayInputItem, UsageMetrics
from services.ai_gateway import AIGateway
from services.tool_registry import tool_registry

class MockProvider:
    def __init__(self):
        self.rounds = 0

    def generate(self, request: GatewayRequest) -> GatewayResponse:
        self.rounds += 1

        if self.rounds == 1:
            from schemas.ai_contracts import ToolCall
            tc = ToolCall(id="call_1", name="mock_tool", arguments='{"param": "value"}')
            return GatewayResponse(
                request_id=request.request_id,
                provider_response_id="response_1",
                model=request.model,
                status="tool_calls_pending",
                tool_calls=[tc],
                latency_ms=10
            )
        else:
            # Check if input has tool result using the proper tool result item type
            tool_res_item = None
            if request.input_items:
                tool_res_item = request.input_items[-1]

            # Also verify chaining via previous_response_id
            chained = getattr(request, "previous_response_id", None)

            # Emulate response
            output_text = "Final Answer. "
            if tool_res_item and tool_res_item.type == "tool_result":
                output_text += f"Tools said: {tool_res_item.text} "
            if chained:
                output_text += f"Chained: {chained}"

            return GatewayResponse(
                request_id=request.request_id,
                model=request.model,
                status="completed",
                text=output_text,
                latency_ms=20
            )

class TestAIGateway(unittest.TestCase):
    def setUp(self):
        @tool_registry.register("mock_tool", "A mock tool", {"type": "object", "properties": {}})
        def mock_tool_func(param: str, context: dict = None):
            return f"Processed: {param}"

        self.provider = MockProvider()
        self.gateway = AIGateway(provider=self.provider)

    def test_gateway_tool_loop(self):
        req = GatewayRequest(
            tenant_id=1,
            channel="widget",
            model="gpt-mock",
            instructions="You are a helpful assistant.",
            input_items=[GatewayInputItem(text="Do something")]
        )

        res = self.gateway.execute(req)

        self.assertEqual(res.status, "completed")
        self.assertTrue("Processed: value" in res.text)
        self.assertTrue("Chained: response_1" in res.text)
        self.assertEqual(self.provider.rounds, 2)
        self.assertEqual(res.latency_ms, 30)

if __name__ == "__main__":
    unittest.main()
