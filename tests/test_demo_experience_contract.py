import unittest

from services.demo_experience_contract import build_demo_experience_contract


class DemoExperienceContractTest(unittest.TestCase):
    def test_municipio_contract_has_first_visit_and_examples(self):
        payload = build_demo_experience_contract(tenant_type="municipio", rubro_label="Municipio Demo")

        self.assertEqual(payload["version"], "2026-05-agent-experience-v2")
        self.assertEqual(payload["tenant_type"], "municipio")
        self.assertTrue(payload["first_visit"]["headline"])
        self.assertTrue(payload["sample_conversations"])
        self.assertTrue(any(item["intent"] == "iniciar_reclamo" for item in payload["quick_actions"]))
        self.assertTrue(payload["lead_capture"]["enabled"])
        self.assertTrue(payload["agent_copilot"]["human_in_the_loop"])
        self.assertTrue(payload["media_capabilities"]["input_modes"]["image"]["enabled"])
        self.assertTrue(payload["media_capabilities"]["input_modes"]["audio"]["enabled"])
        self.assertTrue(payload["media_capabilities"]["input_modes"]["location"]["enabled"])
        self.assertTrue(payload["conversion_ctas"]["actions"])
        self.assertIn("chat.motion.v1", payload["animation_tokens"]["version"])

    def test_pyme_contract_has_sales_experience(self):
        payload = build_demo_experience_contract(tenant_type="pyme", rubro_label="Tienda Demo")

        self.assertEqual(payload["version"], "2026-05-agent-experience-v2")
        self.assertEqual(payload["tenant_type"], "pyme")
        self.assertTrue(any(item["intent"] == "crear_pedido" for item in payload["quick_actions"]))
        self.assertTrue(any(item["intent"] == "crear_pedido" for item in payload["sample_conversations"]))
        self.assertTrue(payload["media_capabilities"]["input_modes"]["file"]["enabled"])
        self.assertEqual(payload["media_capabilities"]["input_modes"]["image"]["upload_endpoint"], "/archivos/upload/chat_attachment")
        self.assertTrue(any(item["intent"] == "checkout_intent" for item in payload["conversion_ctas"]["actions"]))
        section_ids = {item["id"] for item in payload["component_pack"]["sections"]}
        self.assertIn("first_visit", section_ids)
        self.assertIn("media_composer", section_ids)
        self.assertIn("conversion_ctas", section_ids)
        self.assertIn("lead_capture", section_ids)

    def test_education_contract_reuses_pyme_chat_with_school_experience(self):
        payload = build_demo_experience_contract(
            tenant_type="pyme",
            rubro_label="Colegio San Martin",
            vertical="educacion",
            education_profile={"is_education": True, "institution_type": "private", "vertical": "educacion"},
        )

        self.assertEqual(payload["tenant_type"], "pyme")
        self.assertEqual(payload["experience_type"], "education")
        self.assertEqual(payload["vertical"], "educacion")
        self.assertTrue(any(item["intent"] == "justificar_inasistencia" for item in payload["quick_actions"]))
        self.assertTrue(any(item["intent"] == "convivencia_escolar" for item in payload["quick_actions"]))
        self.assertEqual(payload["media_capabilities"]["primary_business_action"], "school_case")
        self.assertIn("certificado_medico", payload["media_capabilities"]["input_modes"]["image"]["intents"])
        self.assertTrue(any(item.get("intent") == "admisiones_colegio" for item in payload["education_quick_menu"]))


if __name__ == "__main__":
    unittest.main()
