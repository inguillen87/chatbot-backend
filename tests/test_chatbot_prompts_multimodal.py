import unittest

from services.chatbot_prompts import get_system_prompt


class ChatbotPromptsMultimodalTest(unittest.TestCase):
    def test_municipio_prompt_includes_multimodal_rules(self):
        prompt = get_system_prompt({"tipo_entidad": "municipio"})

        self.assertIn("Experiencia Multimodal y Conversion", prompt)
        self.assertIn("Sistema operativo del agente Chatboc", prompt)
        self.assertIn("uploaded_file_info", prompt)
        self.assertIn("datos_interpretados_archivo", prompt)
        self.assertIn("ubicacion_compartida", prompt)
        self.assertIn("crear ticket", prompt)
        self.assertIn("template_intent", prompt)
        self.assertIn("gov_claim_status_update", prompt)
        self.assertIn("No digas \"Municipio Inteligente\"", prompt)

    def test_pyme_prompt_includes_multimodal_rules(self):
        prompt = get_system_prompt({"tipo_entidad": "pyme", "pyme_info": {"rubro": "ferreteria"}})

        self.assertIn("Experiencia Multimodal y Conversion", prompt)
        self.assertIn("Sistema operativo del agente Chatboc", prompt)
        self.assertIn("transcribed_text", prompt)
        self.assertIn("crear pedido", prompt)
        self.assertIn("preparar checkout", prompt)
        self.assertIn("dejar contacto", prompt)
        self.assertIn("template_intent", prompt)
        self.assertIn("pyme_catalog_invite", prompt)
        self.assertIn("school_payment_due", prompt)


if __name__ == "__main__":
    unittest.main()
