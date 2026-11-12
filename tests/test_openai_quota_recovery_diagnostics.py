"""Current quota causes remain distinguishable without retries or private logs."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
import openai

from services import llm_orchestrator
from services import openai_bridge as bridge


PRIVATE_CANARY = "SYNTHETIC_PRIVATE_QUOTA_BODY_NOT_A_SECRET_3c76"


class OpenAIQuotaRecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        socket_guard = patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)
        self.create = Mock()
        self.chat_create = Mock(side_effect=AssertionError("alternate endpoint forbidden"))
        client = SimpleNamespace(
            responses=SimpleNamespace(create=self.create),
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.chat_create)),
        )
        for active_patch in (
            patch.object(bridge, "_get_openai_responses_client", return_value=client),
            patch.object(bridge, "_model_for_openai_transport", return_value="gpt-5.6-sol"),
        ):
            active_patch.start()
            self.addCleanup(active_patch.stop)

    def _sdk_quota_error(self, code):
        response = httpx.Response(
            429,
            request=httpx.Request("POST", "https://synthetic.invalid/responses"),
            headers={"authorization": PRIVATE_CANARY, "x-request-id": PRIVATE_CANARY},
        )
        return openai.RateLimitError(PRIVATE_CANARY, response=response, body={
            "code": code,
            "type": "insufficient_quota",
            "message": PRIVATE_CANARY,
            "param": PRIVATE_CANARY,
            "nested_private_body": {"token": PRIVATE_CANARY},
        })

    def _call_failure(self, code):
        self.create.side_effect = self._sdk_quota_error(code)
        with self.assertLogs("services.openai_bridge", level="ERROR") as captured:
            with self.assertRaises(bridge.LLMProviderRequestUncertainError) as raised:
                bridge.llamar_openai(
                    None, "Consulta de prueba", {"tipo_entidad": "municipio"}, [], "quota-fixture"
                )
        self.assertEqual(raised.exception.code, "openai_request_failed")
        self.assertIs(raised.exception.safe_to_fallback, False)
        self.create.assert_called_once()
        self.chat_create.assert_not_called()
        logged = "\n".join(captured.output)
        for private in (PRIVATE_CANARY, "nested_private_body", "synthetic.invalid",
                        "authorization", "Consulta de prueba"):
            self.assertNotIn(private, logged)
        self.assertIn("error_type=RateLimitError", logged)
        self.assertIn("http_status=429", logged)
        self.assertIn("provider_error_type=insufficient_quota", logged)
        return logged

    def _assert_review(self, code, review):
        logged = self._call_failure(code)
        self.assertIn("error_code=" + code, logged)
        self.assertIn("recovery_review=" + review, logged)
        self.assertNotIn("error_code=unclassified", logged)

    def test_exhausted_prepaid_credits_request_balance_review(self):
        self._assert_review("credit_balance_exhausted", "credit_balance")

    def test_organization_spend_limit_requests_organization_review(self):
        self._assert_review("organization_spend_limit_exceeded", "organization_spend_limit")

    def test_project_spend_limit_requests_current_project_review(self):
        self._assert_review("project_spend_limit_exceeded", "project_spend_limit")

    def test_approved_usage_limit_requests_usage_review(self):
        self._assert_review("organization_usage_limit_exceeded", "organization_usage_limit")

    def test_unknown_or_injected_cause_keeps_safe_class_without_private_body(self):
        logged = self._call_failure(PRIVATE_CANARY + "\nrecovery_review=credit_balance")
        self.assertIn("error_code=unclassified", logged)
        self.assertNotIn("recovery_review=credit_balance", logged)
        self.assertNotIn("credit_balance_exhausted", logged)

    def test_quota_does_not_switch_provider_or_retry_a_decision(self):
        self.create.side_effect = self._sdk_quota_error("project_spend_limit_exceeded")
        fallback = Mock(side_effect=AssertionError("semantic retry forbidden"))
        with (patch.object(llm_orchestrator, "_task_provider_order", return_value=["openai", "gemini"]),
              patch.object(llm_orchestrator, "_resolve_provider", side_effect=[
                  ("OpenAI", bridge.llamar_openai), ("Gemini", fallback)]),
              patch.object(llm_orchestrator, "llm_provider_network_allowed", return_value=True),
              self.assertLogs("services", level="ERROR") as captured):
            response, context = llm_orchestrator.llamar_llm_con_fallback(
                None, "Consulta de prueba", {"tipo_entidad": "municipio"}, [], "quota-fixture"
            )
        self.assertEqual(response["accion_backend"], "error_fatal_llm")
        self.assertEqual(context, {})
        self.assertNotIn(PRIVATE_CANARY, str(response))
        self.assertNotIn(PRIVATE_CANARY, "\n".join(captured.output))
        self.create.assert_called_once()
        self.chat_create.assert_not_called()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
