"""Local activation contract regressions. Never provider acceptance."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == "__main__":
    prepare_process()

import base64
from contextlib import ExitStack
import json
import secrets
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from services import twilio_tech_provider as provider
from services import tenant_whatsapp_onboarding as onboarding


class CredentialStorageContractTests(unittest.TestCase):
    def tenant(self, state=None):
        return SimpleNamespace(id=22, slug="test-tenant", nombre="Test tenant",
                               configuracion={provider.STATE_KEY: state or {}},
                               tipo="municipio", plan="full", whatsapp_sender_id=None)

    def config(self, *, store=False):
        config = {"TWILIO_TECH_PROVIDER_LIVE_ENABLED": True,
                  "TWILIO_ACCOUNT_SID": "AC" + secrets.token_hex(16),
                  "TWILIO_AUTH_TOKEN": secrets.token_hex(16),
                  "TWILIO_META_APP_ID": "synthetic-meta-app",
                  "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": "synthetic-signup-config",
                  "RENDER_ENV_SYNC_ENABLED": True}
        if store:
            config.update(TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="v1",
                          TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps(
                              {"v1": base64.b64encode(secrets.token_bytes(32)).decode("ascii")}))
        return config

    def test_crypto_configuration_never_authorizes_new_provider_resources(self):
        with ExitStack() as stack:
            guards = [stack.enter_context(patch.object(provider, name, side_effect=AssertionError("No I/O")))
                      for name in ("_twilio_post_form", "_twilio_post_json", "_twilio_get_json")]
            guards.append(stack.enter_context(patch("services.render_env_sync.sync_render_env_var",
                                                    side_effect=AssertionError("No Render I/O"))))
            result = provider.provision_twilio_subaccount(self.tenant(), {}, self.config(store=True))
        self.assertTrue(result["credential_storage"]["ready"])
        self.assertFalse(result["credential_storage"]["provisioning_ready"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "twilio_provisioning_transaction_unavailable")
        self.assertEqual(result["state_patch"], {})
        self.assertFalse(result["provider_calls_performed"])
        self.assertFalse(result["provider_resources_created"])
        for guard in guards:
            guard.assert_not_called()

    def test_missing_keyring_and_configured_keyring_both_publish_explicit_blockers(self):
        for configured in (False, True):
            with self.subTest(configured=configured):
                contract = provider.build_twilio_tech_provider_contract(self.tenant(), self.config(store=configured))
                storage = contract["automation"]["credential_storage"]
                self.assertEqual(storage["ready"], configured)
                self.assertFalse(storage["provisioning_ready"])
                self.assertEqual(storage["blocked_operations"], ["create_subaccount", "create_messaging_service"])
                self.assertEqual(contract["status"], "needs_secure_activation")
                blocker = contract["setup_health"]["blockers"][0]
                self.assertEqual(blocker["code"], "tenant_secure_activation_pending")
                self.assertEqual(blocker["detail"], storage["message"])
                self.assertEqual(blocker["action"], "wait_for_platform_activation")

    def test_active_workflows_never_offer_a_render_secret_write(self):
        tenant = self.tenant({"render_subaccount_secret_synced": True})
        contract = provider.build_twilio_tech_provider_contract(tenant, self.config())
        private_step = next(step for step in contract["api_workflow"] if step["id"] == "persist_tenant_credentials")
        self.assertIsNone(private_step["endpoint"])
        self.assertEqual(private_step["method"], "INTERNAL")
        self.assertEqual(private_step["state"], "pending_durable_provisioning")
        self.assertNotIn("render", json.dumps(contract["api_workflow"]).lower())
        payload = onboarding._public_payload_for_config(tenant=tenant, contract=contract,
                    state=tenant.configuracion[provider.STATE_KEY], auto_provision_enabled=True, source="test")
        self.assertEqual(payload["status"], "pending_platform_activation")
        self.assertEqual(payload["customer_next_action"], "wait_for_platform_config")
        self.assertEqual(payload["admin_next_action"], "complete_secure_tenant_activation")
        self.assertNotIn("render", json.dumps(payload["steps"]).lower())

    def test_existing_infrastructure_is_preserved_without_declaring_a_live_channel(self):
        state = {"twilio_account_sid": "AC" + secrets.token_hex(16),
                 "messaging_service_sid": "MG" + secrets.token_hex(16)}
        tenant = self.tenant(state)
        contract = provider.build_twilio_tech_provider_contract(tenant, self.config(store=True))
        self.assertEqual(contract["status"], "ready_for_embedded_signup")
        self.assertIsNone(contract["state"]["sender_sid"])
        self.assertEqual(contract["setup_health"]["blockers"][0]["code"], "meta_signup_pending")
        result = provider.provision_twilio_subaccount(tenant, {}, self.config(store=True))
        self.assertTrue(result["idempotent_replay"])
        self.assertFalse(result["provider_resources_created"])
        self.assertEqual(result["state_patch"], {})
        self.assertEqual(tenant.configuracion[provider.STATE_KEY], state)


if __name__ == "__main__":
    unittest.main()
