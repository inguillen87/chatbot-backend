import unittest

from services.chatbot_prompts import get_system_prompt, MUNICIPIO_SYSTEM_PROMPT


class ChatbotPromptTests(unittest.TestCase):
    def test_municipio_prompt_default(self):
        prompt = get_system_prompt({"tipo_entidad": "municipio"})
        self.assertEqual(prompt, MUNICIPIO_SYSTEM_PROMPT)
        self.assertIn('"target": "municipio"', prompt)

    def test_pyme_prompt_includes_demo_context(self):
        usuario = {
            "tipo_entidad": "pyme",
            "pyme_info": {"nombre_pyme": "Bodega Demo", "rubro": "bodega"},
            "demo_contexto": "Producto estrella: Malbec Reserva 2021 ($18.500).",
            "demo_display_name": "Bodega Demo",
        }
        prompt = get_system_prompt(usuario)
        self.assertIn('"target": "pyme"', prompt)
        self.assertIn("Bodega Demo", prompt)
        self.assertIn("Malbec Reserva", prompt)
        self.assertIn("¿Buscás por precio, marca o uso?", prompt)
        self.assertIn("Experiencia Omnicanal", prompt)


if __name__ == "__main__":
    unittest.main()
