import unittest
from types import SimpleNamespace

from services.plan_access import (
    integration_access_payload,
    integration_feature_payload,
    integration_plan_required_payload,
    plan_allows_integration_feature,
    plan_allows_full_integrations,
)


def tenant_stub(
    *,
    plan="free",
    is_active=True,
    configuracion=None,
    capabilities=None,
    id=30,
    slug="chatboc-demo",
):
    return SimpleNamespace(
        id=id,
        slug=slug,
        plan=plan,
        is_active=is_active,
        configuracion=configuracion or {},
        capabilities=capabilities,
        capabilities_json=None,
    )


class PlanAccessContractTest(unittest.TestCase):
    def test_free_plan_enables_self_service_features_and_locks_productive_channels(self):
        payload = integration_access_payload(tenant_stub(plan="free"))

        self.assertEqual(payload["contract_version"], "tenant.integration_access.v1")
        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["status"], "partial")
        self.assertEqual(payload["reason_code"], "plan_full_required")
        self.assertEqual(payload["lock_reason_code"], "plan_full_required")
        self.assertFalse(payload["frontend_contract"]["render_locked_state"])
        self.assertTrue(payload["frontend_contract"]["self_service_enabled"])
        self.assertTrue(payload["frontend_contract"]["productive_channels_locked"])
        self.assertTrue(payload["frontend_contract"]["hide_embed_copy"])
        self.assertFalse(payload["features"]["widget_embed"]["enabled"])
        self.assertFalse(payload["features"]["whatsapp_business_platform"]["enabled"])
        self.assertFalse(payload["features"]["mercadopago_checkout"]["enabled"])
        self.assertTrue(payload["features"]["catalog_management"]["enabled"])
        self.assertTrue(payload["features"]["analytics_dashboard"]["enabled"])
        self.assertTrue(payload["features"]["heatmaps"]["enabled"])
        self.assertTrue(payload["features"]["surveys_votings"]["enabled"])
        self.assertTrue(payload["features"]["comments_inbox"]["enabled"])
        self.assertTrue(payload["features"]["education_management"]["enabled"])
        self.assertTrue(plan_allows_integration_feature(tenant_stub(plan="free"), "catalog_management"))
        self.assertFalse(plan_allows_integration_feature(tenant_stub(plan="free"), "whatsapp_sender_management"))
        self.assertIn("connect_whatsapp_sender", payload["blocked_actions"])
        self.assertIn("configure_payment_gateway", payload["blocked_actions"])
        self.assertIn("create_surveys", payload["allowed_actions"])
        self.assertIn("run_analytics_dashboard", payload["allowed_actions"])
        self.assertIn("open_heatmap", payload["allowed_actions"])
        self.assertIn("manage_education_operations", payload["allowed_actions"])

    def test_full_plan_enables_productive_integration_features(self):
        tenant = tenant_stub(plan="full")
        payload = integration_access_payload(tenant)

        self.assertTrue(plan_allows_full_integrations(tenant))
        self.assertTrue(payload["enabled"])
        self.assertEqual(payload["status"], "enabled")
        self.assertIsNone(payload["reason_code"])
        self.assertFalse(payload["frontend_contract"]["hide_provider_connect"])
        self.assertTrue(payload["features"]["widget_embed"]["enabled"])
        self.assertTrue(payload["features"]["whatsapp_business_platform"]["enabled"])
        self.assertTrue(payload["features"]["mercadopago_checkout"]["enabled"])
        self.assertTrue(payload["features"]["analytics_dashboard"]["enabled"])
        self.assertTrue(payload["features"]["heatmaps"]["enabled"])
        self.assertTrue(payload["features"]["surveys_votings"]["enabled"])
        self.assertTrue(payload["features"]["comments_inbox"]["enabled"])
        self.assertTrue(payload["features"]["education_management"]["enabled"])
        self.assertIn("copy_widget_embed", payload["allowed_actions"])
        self.assertIn("configure_payment_gateway", payload["allowed_actions"])
        self.assertIn("create_surveys", payload["allowed_actions"])
        self.assertIn("run_analytics_dashboard", payload["allowed_actions"])
        self.assertIn("open_heatmap", payload["allowed_actions"])
        self.assertIn("manage_education_operations", payload["allowed_actions"])

    def test_demo_context_remains_locked_even_with_full_plan(self):
        payload = integration_access_payload(
            tenant_stub(plan="full", configuracion={"demo_mode": True})
        )

        self.assertFalse(payload["enabled"])
        self.assertEqual(payload["reason_code"], "plan_full_required")
        self.assertEqual(payload["lock_reason_code"], "demo_tenant_locked")
        self.assertFalse(payload["features"]["widget_embed"]["enabled"])
        self.assertFalse(payload["features"]["catalog_management"]["enabled"])
        self.assertTrue(payload["security"]["demo_tenants_blocked"])

    def test_plan_required_payload_exposes_widget_frontend_contract(self):
        tenant = tenant_stub(plan="free")
        payload = integration_plan_required_payload(
            tenant,
            "widget_embed",
            contract_version="public.widget_user.v1",
            hide_embed_copy=True,
            hide_widget_session=True,
        )

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "plan_required")
        self.assertEqual(payload["status_code"], 403)
        self.assertEqual(payload["contract_version"], "public.widget_user.v1")
        self.assertEqual(payload["tenant_slug"], "chatboc-demo")
        self.assertEqual(payload["feature_id"], "widget_embed")
        self.assertEqual(payload["feature"]["action"], "copy_widget_embed")
        self.assertEqual(payload["frontend_contract"]["feature_id"], "widget_embed")
        self.assertEqual(payload["frontend_contract"]["render_as"], "integration_locked")
        self.assertTrue(payload["frontend_contract"]["hide_embed_copy"])
        self.assertTrue(payload["frontend_contract"]["hide_widget_session"])
        self.assertEqual(payload["frontend_contract"]["primary_action"], "upgrade_to_full")
        self.assertTrue(payload["access"]["security"]["demo_tenants_blocked"])

    def test_plan_required_payload_uses_specific_feature_metadata(self):
        payload = integration_plan_required_payload(tenant_stub(plan="free"), "surveys_votings")

        self.assertEqual(payload["feature"]["action"], "create_surveys")
        self.assertEqual(payload["frontend_contract"]["feature_label"], "Encuestas y votaciones")

    def test_unknown_feature_payload_is_stable(self):
        access = integration_access_payload(tenant_stub(plan="free"))
        feature = integration_feature_payload(access, "future_feature")

        self.assertEqual(feature["id"], "future_feature")
        self.assertFalse(feature["enabled"])
        self.assertEqual(feature["capability"], "integrations.production")
        self.assertEqual(feature["reason_code"], "plan_full_required")


if __name__ == "__main__":
    unittest.main()
