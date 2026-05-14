import unittest

from services.landing_experience_contract import (
    LANDING_EXPERIENCE_CONTRACT_VERSION,
    build_landing_experience_contract,
)


class _Tenant:
    slug = "colegio-demo"
    tipo = "pyme"
    nombre = "Colegio Demo"
    logo_url = "https://example.com/logo.png"
    vertical = "educacion"
    subvertical = "colegio"
    tema = {"primary": "#0ea5e9", "accent": "#22c55e"}
    configuracion = {"education": {"enabled": True}}
    capabilities_json = {"education": {"enabled": True}}


class LandingExperienceContractTestCase(unittest.TestCase):
    def test_platform_landing_contract_has_operational_payload_only(self):
        payload = build_landing_experience_contract()

        self.assertEqual(payload["contract_version"], LANDING_EXPERIENCE_CONTRACT_VERSION)
        self.assertEqual(payload["experience_kind"], "platform")
        self.assertNotIn("brand", payload)
        self.assertNotIn("design_tokens", payload)
        self.assertNotIn("motion", payload)
        self.assertNotIn("navigation", payload)
        self.assertNotIn("sections", payload)
        self.assertEqual(payload["hero"]["contract_scope"], "operational_demo_data")
        self.assertEqual(payload["hero"]["headline"], "Converti conversaciones en operaciones reales")
        self.assertEqual(
            payload["hero"]["subheadline"],
            "Chatboc atiende por web o WhatsApp, pide los datos justos y deja casos, pedidos o leads listos para operar.",
        )
        self.assertEqual(payload["hero"]["conversation_title"], "WhatsApp operativo")
        self.assertEqual(
            payload["hero"]["conversation_subtitle"],
            "Un caso entra, el agente pide datos y deja una accion trazable.",
        )
        self.assertEqual(payload["hero"]["primary_cta"]["href"], "/demo")
        self.assertEqual(payload["hero"]["secondary_cta"]["href"], "/contacto")
        self.assertEqual(payload["hero"]["conversation_demo"]["contract_version"], "landing.hero_conversation_demo.v1")
        self.assertTrue(payload["runtime_rules"]["frontend_owns_visual_design"])
        self.assertTrue(payload["runtime_rules"]["backend_owns_visible_copy"])
        self.assertTrue(payload["runtime_rules"]["backend_owns_sessions_actions_and_traceability"])
        self.assertTrue(payload["runtime_rules"]["do_not_publish_frontend_mock_data"])
        self.assertTrue(payload["hero"]["workflow_steps"])
        flows = payload["hero"]["conversation_demo"]["flows"]
        self.assertGreaterEqual(len(flows), 4)
        for flow in flows:
            self.assertTrue(flow.get("action"))
            self.assertTrue(flow.get("result", {}).get("traceable"))
            self.assertTrue(flow.get("cta", {}).get("href"))
            self.assertTrue(flow.get("workflow_steps"))
            self.assertTrue(flow.get("admin_preview_endpoint"))
            self.assertTrue(flow["action"].get("fields"))
            self.assertTrue(flow["action"].get("summary_items"))
            self.assertTrue(flow["action"].get("facts"))
            self.assertTrue(flow["action"].get("details"))
            self.assertTrue(flow["action"].get("attributes"))
            self.assertTrue(flow["action"].get("metadata", {}).get("traceable_target"))
        self.assertIn("gobierno-reclamo-ubicacion", {flow["id"] for flow in flows})
        self.assertIn("pyme-pedido-carrito", {flow["id"] for flow in flows})
        self.assertIn("colegio-certificado-caso", {flow["id"] for flow in flows})
        self.assertIn("encuestas-votacion-en-vivo", {flow["id"] for flow in flows})
        gobierno_flow = next(flow for flow in flows if flow["id"] == "gobierno-reclamo-ubicacion")
        location_input = next(item for item in gobierno_flow["inputs"] if item["kind"] == "location")
        image_input = next(item for item in gobierno_flow["inputs"] if item["kind"] == "image")
        self.assertIn("address", location_input)
        self.assertIn("lat", location_input)
        self.assertIn("lng", location_input)
        self.assertNotIn("preview_url", image_input)
        self.assertNotIn("image_url", image_input)
        self.assertEqual(payload["conversion"]["lead_capture_endpoint"], "/api/public/lead-capture")
        self.assertEqual(payload["conversion"]["lead_capture"]["endpoint"], "/api/public/lead-capture")
        self.assertTrue(payload["conversion"]["lead_capture"]["fields"])
        self.assertEqual(payload["conversion"]["lead_capture"]["required_fields"], ["name"])
        self.assertEqual(payload["conversion"]["lead_capture"]["required_any_of"], [["phone", "email"]])
        self.assertTrue(payload["conversion"]["lead_capture"]["progressive_capture"]["enabled"])
        self.assertEqual(payload["conversion"]["lead_capture"]["success_state"]["contract_version"], "public.lead_capture.success.v1")
        self.assertEqual(payload["conversion"]["lead_capture"]["validation_state"]["render_as"], "inline_field_errors")
        self.assertEqual(payload["conversion"]["demo_session_endpoint"], "/api/v2/demo/session")
        self.assertEqual(payload["conversion"]["admin_preview_endpoint"], "/api/v2/demo/admin-preview")
        journey = payload["conversion"]["journey"]
        self.assertEqual(journey["contract_version"], "public.conversion_journey.v1")
        self.assertEqual(journey["goal"], "convert_visitor_to_demo_lead_or_tenant")
        self.assertTrue(any(step["id"] == "start_demo_session" for step in journey["steps"]))
        self.assertTrue(any(step["id"] == "capture_lead" for step in journey["steps"]))
        self.assertTrue(any(step["id"] == "tenant_signup_intent" for step in journey["steps"]))
        self.assertTrue(any(step["id"] == "activate_whatsapp_after_tenant_exists" for step in journey["steps"]))
        self.assertTrue(journey["frontend_rules"]["do_not_post_empty_leads"])
        self.assertTrue(journey["frontend_rules"]["do_not_show_twilio_console_steps"])

    def test_landing_experience_returns_traceable_conversation_demo(self):
        payload = build_landing_experience_contract()
        flows = payload["hero"]["conversation_demo"]["flows"]

        self.assertTrue(flows)
        self.assertTrue(all(flow.get("action") for flow in flows))
        self.assertTrue(all((flow.get("result") or {}).get("traceable") is True for flow in flows))
        for flow in flows:
            for item in flow.get("inputs") or []:
                if item.get("kind") == "image":
                    self.assertNotIn("preview_url", item)
                    self.assertNotIn("thumbnail_url", item)
                    self.assertNotIn("image_url", item)
            self.assertNotIn("metrics", flow)

    def test_tenant_landing_contract_keeps_only_tenant_identity_and_operational_flows(self):
        payload = build_landing_experience_contract(_Tenant(), page="colegios")

        self.assertEqual(payload["experience_kind"], "educacion")
        self.assertTrue(payload["tenant"]["white_label"])
        self.assertEqual(payload["tenant"]["slug"], "colegio-demo")
        self.assertEqual(payload["selected_page"], "colegios")
        self.assertNotIn("brand", payload)
        self.assertNotIn("design_tokens", payload)
        self.assertEqual(payload["hero"]["conversation_demo"]["flows"][0]["id"], "colegio-certificado-caso")


if __name__ == "__main__":
    unittest.main()
