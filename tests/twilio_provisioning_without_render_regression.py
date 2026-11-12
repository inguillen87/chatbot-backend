"""No remote provisioning may precede durable tenant credential storage.

These are local side-effect regressions, not provider acceptance.
"""
from copy import deepcopy
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import twilio_tech_provider as provider


class ProvisioningWithoutRenderTests(unittest.TestCase):
    def run_plan(self, state, live=True):
        original = deepcopy(state)
        tenant = SimpleNamespace(id=22, slug="test-tenant", nombre="Test tenant",
                                 configuracion={provider.STATE_KEY: state},
                                 tipo="municipio", plan="full", whatsapp_sender_id=None)
        config = {"TWILIO_TECH_PROVIDER_LIVE_ENABLED": live,
                  "RENDER_ENV_SYNC_ENABLED": True,
                  "TWILIO_ACCOUNT_SID": "synthetic-parent-account",
                  "TWILIO_AUTH_TOKEN": "synthetic-parent-credential",
                  "RENDER_API_KEY": "synthetic-render-credential"}
        with ExitStack() as stack:
            guards = [stack.enter_context(patch.object(provider, name, side_effect=AssertionError("No provider calls")))
                      for name in ("_twilio_post_form", "_twilio_post_json", "_twilio_get_json")]
            guards.append(stack.enter_context(patch("services.render_env_sync.sync_render_env_var",
                                                    side_effect=AssertionError("No Render calls"))))
            result = provider.provision_twilio_subaccount(tenant, {"phone_number": "+15555550123"}, config)
            for guard in guards:
                guard.assert_not_called()
        self.assertEqual(state, original)
        self.assertEqual(result["state_patch"], {})
        self.assertFalse(result["provider_calls_performed"])
        self.assertFalse(result["provider_resources_created"])
        return result

    def test_new_live_connection_stops_before_creating_remote_resources(self):
        result = self.run_plan({})
        self.assertFalse(result["ok"])
        self.assertEqual(result["mode"], "blocked")
        self.assertEqual(result["reason_code"], "twilio_tenant_credential_store_unavailable")
        self.assertFalse(result["credential_storage"]["ready"])

    def test_existing_account_cannot_create_service_without_durable_credentials(self):
        result = self.run_plan({"twilio_account_sid": "synthetic-existing-account"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "twilio_tenant_credential_store_unavailable")

    def test_existing_connected_references_replay_without_mutation_or_new_readiness(self):
        result = self.run_plan({"twilio_account_sid": "synthetic-existing-account",
                                "messaging_service_sid": "synthetic-existing-service",
                                "status": "sender_online", "sender_status": "ONLINE"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["idempotent_replay"])
        self.assertFalse(result["credential_storage"]["ready"])
        self.assertNotIn("embedded_signup", [step["id"] for step in result["steps"]])

    def test_service_without_account_stays_inconsistent(self):
        result = self.run_plan({"messaging_service_sid": "synthetic-existing-service"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "twilio_provisioning_state_inconsistent")

    def test_dry_plan_never_degrades_existing_connection(self):
        result = self.run_plan({"status": "sender_online", "sender_status": "ONLINE"}, live=False)
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["credential_storage"]["ready"])
        self.assertTrue(all(step["status"] != "done" for step in result["steps"]))


if __name__ == "__main__":
    unittest.main()
