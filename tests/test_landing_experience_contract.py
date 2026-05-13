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
    def test_platform_landing_contract_has_core_ux_payload(self):
        payload = build_landing_experience_contract()

        self.assertEqual(payload["contract_version"], LANDING_EXPERIENCE_CONTRACT_VERSION)
        self.assertEqual(payload["experience_kind"], "platform")
        self.assertEqual(payload["hero"]["h1"], "Chatboc")
        self.assertEqual(payload["hero"]["conversation_demo"]["contract_version"], "landing.hero_conversation_demo.v1")
        self.assertTrue(payload["hero"]["workflow_steps"])
        flows = payload["hero"]["conversation_demo"]["flows"]
        self.assertGreaterEqual(len(flows), 4)
        for flow in flows:
            self.assertTrue(flow.get("action"))
            self.assertTrue(flow.get("result", {}).get("traceable"))
            self.assertTrue(flow.get("cta", {}).get("href"))
            self.assertTrue(flow.get("workflow_steps"))
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
        self.assertIn("preview_url", image_input)
        self.assertIn("image_url", image_input)
        self.assertTrue(payload["hero"]["media"]["assets"])
        self.assertIn("primary", payload["design_tokens"]["color"])
        self.assertIn("accent", payload["design_tokens"]["color"])
        self.assertIn("warm", payload["design_tokens"]["color"])
        self.assertEqual(payload["motion"]["contract_version"], "landing.motion.v1")

        page_ids = {page["id"] for page in payload["adjacent_pages"]}
        self.assertIn("demo", page_ids)
        self.assertIn("colegios", page_ids)
        self.assertIn("widget", page_ids)

    def test_tenant_landing_contract_uses_white_label_brand(self):
        payload = build_landing_experience_contract(_Tenant(), page="colegios")

        self.assertEqual(payload["experience_kind"], "educacion")
        self.assertTrue(payload["tenant"]["white_label"])
        self.assertEqual(payload["tenant"]["slug"], "colegio-demo")
        self.assertEqual(payload["brand"]["wordmark"], "Colegio Demo")
        self.assertEqual(payload["brand"]["logo"]["source"], "tenant")
        self.assertEqual(payload["selected_page"], "colegios")
        self.assertEqual(payload["design_tokens"]["color"]["primary"], "#0ea5e9")
        self.assertEqual(payload["hero"]["conversation_demo"]["flows"][0]["id"], "colegio-certificado-caso")


if __name__ == "__main__":
    unittest.main()
