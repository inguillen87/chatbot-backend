import unittest

from utils.response_utils import ensure_buttons_compatibility


class EnsureButtonsCompatibilityTest(unittest.TestCase):
    def test_copies_label_to_texto_and_generates_id(self):
        payload = {
            "options_list": [
                {"label": "Ver promociones", "action_id": "pyme_promociones"}
            ]
        }

        result = ensure_buttons_compatibility(payload)

        self.assertIs(result, payload)
        botones = result.get("botones")
        self.assertIsInstance(botones, list)
        self.assertEqual(botones[0]["texto"], "Ver promociones")
        self.assertEqual(botones[0]["action_id"], "pyme_promociones")
        self.assertEqual(botones[0]["id"], "pyme_promociones")

    def test_uses_id_field_when_action_id_missing(self):
        payload = {"botones": [{"texto": "Ir al sitio", "id": "open_url"}]}

        result = ensure_buttons_compatibility(payload)

        opciones = result.get("options_list")
        self.assertEqual(opciones[0]["action_id"], "open_url")
        self.assertEqual(opciones[0]["id"], "open_url")

    def test_defaults_action_id_to_text(self):
        payload = {"options_list": [{"texto": "Contáctanos"}]}

        result = ensure_buttons_compatibility(payload)

        boton = result["botones"][0]
        self.assertEqual(boton["texto"], "Contáctanos")
        self.assertEqual(boton["action_id"], "Contáctanos")
        self.assertEqual(boton["id"], "Contáctanos")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
