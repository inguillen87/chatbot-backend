import unittest
from types import SimpleNamespace

from providers.openai_responses import OpenAIResponsesProvider
from schemas.ai_contracts import GatewayInputItem, GatewayRequest


class _FakeResponses:
    def __init__(self, response):
        self.response = response
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.responses = _FakeResponses(response)


class OpenAIResponsesProviderTestCase(unittest.TestCase):
    def test_generate_uses_responses_api_with_structured_output(self):
        response = SimpleNamespace(
            id="resp_123",
            model="gpt-5.5",
            status="completed",
            output_text='{"ok": true}',
            output=[],
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=7,
                total_tokens=18,
                input_tokens_details=SimpleNamespace(cached_tokens=3),
            ),
        )
        client = _FakeClient(response)
        provider = OpenAIResponsesProvider(client=client)
        request = GatewayRequest(
            tenant_id=1,
            channel="web",
            model="gpt-5.5",
            instructions="Respondé JSON.",
            input_items=[GatewayInputItem(type="text", text="hola")],
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
        )

        result = provider.generate(request)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.structured_output, {"ok": True})
        self.assertEqual(result.usage.total_tokens, 18)
        self.assertEqual(result.usage.cached_tokens, 3)
        kwargs = client.responses.last_kwargs
        self.assertEqual(kwargs["model"], "gpt-5.5")
        self.assertEqual(kwargs["instructions"], "Respondé JSON.")
        self.assertEqual(kwargs["input"][0]["content"][0]["type"], "input_text")
        self.assertEqual(kwargs["text"]["format"]["type"], "json_schema")

    def test_generate_maps_responses_function_calls(self):
        response = SimpleNamespace(
            id="resp_tool",
            model="gpt-5.5",
            status="completed",
            output_text=None,
            output=[
                SimpleNamespace(
                    type="function_call",
                    call_id="call_123",
                    name="crear_reclamo",
                    arguments='{"categoria": "bache"}',
                )
            ],
            usage=None,
        )
        client = _FakeClient(response)
        provider = OpenAIResponsesProvider(client=client)
        request = GatewayRequest(
            tenant_id=1,
            channel="web",
            model="gpt-5.5",
            instructions="Usá herramientas.",
            input_items=[GatewayInputItem(type="text", text="crear reclamo")],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "crear_reclamo",
                        "description": "Crea un reclamo.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        )

        result = provider.generate(request)

        self.assertEqual(result.status, "tool_calls_pending")
        self.assertEqual(result.tool_calls[0].id, "call_123")
        self.assertEqual(result.tool_calls[0].name, "crear_reclamo")
        kwargs = client.responses.last_kwargs
        self.assertEqual(kwargs["tools"][0]["type"], "function")
        self.assertEqual(kwargs["tools"][0]["name"], "crear_reclamo")
        self.assertNotIn("function", kwargs["tools"][0])


if __name__ == "__main__":
    unittest.main()
