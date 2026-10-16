import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from providers.openai_responses import OpenAIResponsesProvider
from schemas.ai_contracts import GatewayInputItem, GatewayRequest
from services import openai_bridge


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
    def tearDown(self):
        openai_bridge._reset_openai_client_for_tests()

    def test_managed_client_uses_cloudflare_transport_model(self):
        response = SimpleNamespace(
            id="resp_gateway",
            model="openai/gpt-5.6-sol",
            status="completed",
            output_text="ok",
            output=[],
            usage=None,
        )
        client = _FakeClient(response)
        captured = {}

        def _fake_openai(**kwargs):
            captured.update(kwargs)
            return client

        gateway_env = {
            "CLOUDFLARE_AI_GATEWAY_ENABLED": "true",
            "CLOUDFLARE_AI_GATEWAY_ACCOUNT_ID": "b" * 32,
            "CLOUDFLARE_AI_GATEWAY_API_TOKEN": "cloudflare-provider-test-token",
            "CLOUDFLARE_AI_GATEWAY_ID": "default",
            "OPENAI_ALLOW_NETWORK_IN_TESTS": "1",
        }
        openai_bridge._reset_openai_client_for_tests()
        with (
            patch.dict(os.environ, gateway_env, clear=False),
            patch.object(openai_bridge, "OpenAI", _fake_openai),
        ):
            provider = OpenAIResponsesProvider()
            result = provider.generate(
                GatewayRequest(
                    tenant_id=1,
                    channel="web",
                    model="gpt-5.6-sol",
                    instructions="Respondé breve.",
                    input_items=[GatewayInputItem(type="text", text="hola")],
                )
            )

        self.assertEqual(result.status, "completed")
        self.assertEqual(client.responses.last_kwargs["model"], "openai/gpt-5.6-sol")
        self.assertIs(client.responses.last_kwargs["store"], False)
        self.assertEqual(
            captured["base_url"],
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{'b' * 32}/ai/v1",
        )
        self.assertEqual(
            captured["default_headers"]["cf-aig-collect-log-payload"],
            "false",
        )

    def test_generate_stream_uses_responses_stream_and_preserves_typed_events(self):
        final_response = SimpleNamespace(
            id="resp_stream",
            model="gpt-5.6-sol",
            status="completed",
            output_text="Hola, Marcelo",
            output=[],
            usage=None,
        )
        manager = MagicMock()
        manager.__enter__.return_value = manager
        manager.__exit__.return_value = False
        manager.__iter__.return_value = iter(
            [
                SimpleNamespace(type="response.output_text.delta", delta="Hola, "),
                SimpleNamespace(type="response.output_text.delta", delta="Marcelo"),
            ]
        )
        manager.get_final_response.return_value = final_response
        responses_stream = MagicMock(return_value=manager)
        chat_create = MagicMock()
        client = SimpleNamespace(
            responses=SimpleNamespace(stream=responses_stream, create=MagicMock()),
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        )
        provider = OpenAIResponsesProvider(client=client)
        request = GatewayRequest(
            tenant_id=1,
            channel="web",
            model="gpt-5.6-sol",
            instructions="Respondé breve.",
            input_items=[GatewayInputItem(type="text", text="hola")],
        )

        events = list(provider.generate_stream(request))

        self.assertEqual(
            [event["type"] for event in events],
            ["response.delta", "response.delta", "response.completed"],
        )
        self.assertEqual(events[-1]["data"]["text"], "Hola, Marcelo")
        self.assertEqual(events[-1]["data"]["provider_response_id"], "resp_stream")
        responses_stream.assert_called_once()
        chat_create.assert_not_called()

    def test_generate_stream_does_not_fallback_after_responses_stream_failure(self):
        responses_stream = MagicMock(side_effect=TimeoutError("sensitive stream payload"))
        chat_create = MagicMock()
        client = SimpleNamespace(
            responses=SimpleNamespace(stream=responses_stream, create=MagicMock()),
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        )
        provider = OpenAIResponsesProvider(client=client)
        request = GatewayRequest(
            tenant_id=1,
            channel="web",
            model="gpt-5.6-sol",
            instructions="Respondé breve.",
            input_items=[GatewayInputItem(type="text", text="hola")],
        )

        with self.assertLogs("providers.openai_responses", level="ERROR") as logs:
            events = list(provider.generate_stream(request))

        self.assertEqual(events[0]["type"], "response.error")
        self.assertEqual(events[0]["data"]["error_code"], "provider_outcome_unknown")
        self.assertNotIn("sensitive", events[0]["data"]["message"])
        self.assertNotIn("sensitive stream", "\n".join(logs.output))
        chat_create.assert_not_called()

    def test_generate_does_not_replay_ambiguous_responses_failure_via_chat_completions(self):
        responses_create = MagicMock(
            side_effect=TimeoutError("sensitive prompt or credential material")
        )
        chat_create = MagicMock()
        client = SimpleNamespace(
            responses=SimpleNamespace(create=responses_create),
            chat=SimpleNamespace(completions=SimpleNamespace(create=chat_create)),
        )
        provider = OpenAIResponsesProvider(client=client)
        request = GatewayRequest(
            tenant_id=1,
            channel="whatsapp",
            model="gpt-5.6-sol",
            instructions="Respondé con seguridad.",
            input_items=[GatewayInputItem(type="text", text="crear reclamo")],
        )

        with self.assertLogs("providers.openai_responses", level="ERROR") as logs:
            result = provider.generate(request)

        self.assertEqual(result.status, "error")
        self.assertEqual(result.error_code, "provider_outcome_unknown")
        self.assertNotIn("sensitive", result.error_message)
        responses_create.assert_called_once()
        chat_create.assert_not_called()
        self.assertNotIn("sensitive prompt", "\n".join(logs.output))

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
