"""The real Responses boundary must expose only bounded SDK failure metadata."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import openai

from services import llm_orchestrator
from services import openai_bridge as bridge


CANARY = "SYNTHETIC_PRIVATE_BODY_NOT_A_REAL_SECRET_b349"


class OpenAIProviderErrorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        network = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        self.create = Mock()
        self.chat_create = Mock(side_effect=AssertionError("alternate endpoint forbidden"))
        self.client = SimpleNamespace(
            responses=SimpleNamespace(create=self.create),
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.chat_create)),
        )
        client_patch = patch.object(bridge, "_get_openai_responses_client", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        model_patch = patch.object(bridge, "_model_for_openai_transport", return_value="gpt-5.6-sol")
        model_patch.start()
        self.addCleanup(model_patch.stop)

    def _sdk_error(self, cls=openai.RateLimitError, *, status=429, code=None, error_type=None):
        response = httpx.Response(
            status, request=httpx.Request("POST", "https://synthetic.invalid/responses"),
            headers={"authorization": CANARY, "x-request-id": CANARY},
        )
        return cls(CANARY, response=response, body={
            "code": code, "type": error_type, "message": CANARY,
            "param": CANARY, "nested_private_body": {"token": CANARY},
        })

    def _call_failure(self, error):
        self.create.reset_mock(side_effect=True)
        self.create.side_effect = error
        with self.assertLogs("services.openai_bridge", level="ERROR") as captured:
            with self.assertRaises(bridge.LLMProviderRequestUncertainError) as raised:
                bridge.llamar_openai(None, "Pregunta sintética", {"tipo_entidad": "municipio"}, [], "synthetic-session")
        self.assertEqual(raised.exception.code, "openai_request_failed")
        self.assertIs(raised.exception.safe_to_fallback, False)
        self.create.assert_called_once()
        self.chat_create.assert_not_called()
        logged = "\n".join(captured.output)
        for private in (CANARY, "nested_private_body", "synthetic.invalid", "authorization", "Pregunta sintética"):
            self.assertNotIn(private, logged)
        return logged

    def test_rate_limit_error_has_safe_status_and_cause(self):
        for code, error_type in (("insufficient_quota", "insufficient_quota"),
                                 ("rate_limit_exceeded", "rate_limit_error")):
            with self.subTest(code=code):
                logged = self._call_failure(self._sdk_error(code=code, error_type=error_type))
                self.assertIn("http_status=429", logged)
                self.assertIn("error_type=RateLimitError", logged)
                self.assertIn("error_code=" + code, logged)
                self.assertIn("provider_error_type=" + error_type, logged)

    def test_auth_error_is_classified_without_disclosing_body(self):
        logged = self._call_failure(self._sdk_error(openai.AuthenticationError, status=401,
                                    code="invalid_api_key", error_type="invalid_request_error"))
        self.assertIn("http_status=401", logged)
        self.assertIn("error_type=AuthenticationError", logged)
        self.assertIn("error_code=invalid_api_key", logged)

    def test_missing_server_code_stays_unclassified(self):
        logged = self._call_failure(self._sdk_error())
        self.assertIn("http_status=429", logged)
        self.assertIn("error_code=unclassified", logged)
        self.assertIn("provider_error_type=unclassified", logged)
        self.assertNotIn("insufficient_quota", logged)
        self.assertNotIn("rate_limit_exceeded", logged)

    def test_unknown_nonstring_or_injected_server_fields_are_not_logged(self):
        for value in (CANARY, CANARY + "\nerror_code=insufficient_quota", {"private": CANARY},
                      [CANARY], 429, "INSUFFICIENT_QUOTA"):
            with self.subTest(value_type=type(value).__name__):
                logged = self._call_failure(self._sdk_error(code=value, error_type=value))
                self.assertIn("error_code=unclassified", logged)
                self.assertIn("provider_error_type=unclassified", logged)

    def test_invalid_status_or_dynamic_exception_class_is_not_logged(self):
        for status in (None, True, "429", 99, 600, 429.0, CANARY):
            with self.subTest(status_type=type(status).__name__):
                error_class = type(CANARY, (Exception,), {"status_code": status, "code": CANARY, "type": CANARY})
                logged = self._call_failure(error_class(CANARY))
                self.assertIn("http_status=None", logged)
                self.assertIn("error_type=Exception", logged)
                self.assertIn("error_code=unclassified", logged)

    def test_diagnostic_properties_cannot_replace_safe_failure_or_read_body(self):
        class SyntheticError(Exception):
            @property
            def status_code(self):
                raise RuntimeError(CANARY)

            @property
            def code(self):
                raise RuntimeError(CANARY)

            @property
            def type(self):
                raise RuntimeError(CANARY)

            @property
            def body(self):
                raise AssertionError("must not inspect body")

            @property
            def response(self):
                raise AssertionError("must not inspect response or headers")

        logged = self._call_failure(SyntheticError(CANARY))
        self.assertIn("http_status=None", logged)
        self.assertIn("error_code=unclassified", logged)
        self.assertIn("provider_error_type=unclassified", logged)

    def test_real_orchestrator_keeps_uncertain_failure_closed(self):
        self.create.side_effect = self._sdk_error(code="insufficient_quota", error_type="insufficient_quota")
        fallback = Mock(side_effect=AssertionError("semantic retry forbidden"))
        with (patch.object(llm_orchestrator, "_task_provider_order", return_value=["openai", "gemini"]),
              patch.object(llm_orchestrator, "_resolve_provider", side_effect=[("OpenAI", bridge.llamar_openai), ("Gemini", fallback)]),
              patch.object(llm_orchestrator, "llm_provider_network_allowed", return_value=True),
              self.assertLogs("services", level="ERROR") as captured):
            response, context = llm_orchestrator.llamar_llm_con_fallback(
                None, "Pregunta sintética", {"tipo_entidad": "municipio"}, [], "synthetic-session")
        self.assertEqual(response["accion_backend"], "error_fatal_llm")
        self.assertEqual(context, {})
        self.assertNotIn(CANARY, str(response))
        self.assertNotIn(CANARY, "\n".join(captured.output))
        self.create.assert_called_once()
        fallback.assert_not_called()
        self.chat_create.assert_not_called()
