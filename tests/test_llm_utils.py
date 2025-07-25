import unittest
from unittest.mock import patch, MagicMock
import json

# Ensure the services package is discoverable.
# This might require adjusting PYTHONPATH or how tests are run in some environments.
# For now, assuming direct importability or that the test runner handles paths.
try:
    from services.llm_utils import (
        extract_multiple_contact_details_llm,
        extract_complaint_details_llm,
        update_summary_with_llm_extraction,
        _clean_llm_json_output
    )
except ImportError:
    # Fallback for local testing if path issues occur, adjust as necessary
    import sys
    import os
    sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    from services.llm_utils import (
        extract_multiple_contact_details_llm,
        extract_complaint_details_llm,
        update_summary_with_llm_extraction,
        _clean_llm_json_output
    )

class TestLLMUtils(unittest.TestCase):

    def test_clean_llm_json_output(self):
        self.assertEqual(_clean_llm_json_output("```json\n{\"key\": \"value\",}\n```"), "{\"key\": \"value\"}")
        self.assertEqual(_clean_llm_json_output("  ```json\n{\"key\": \"value\"}  ```  "), "{\"key\": \"value\"}")
        self.assertEqual(_clean_llm_json_output("{\"key\": \"value\",}"), "{\"key\": \"value\"}")
        self.assertEqual(_clean_llm_json_output("{\"key\": [1,2,],}"), "{\"key\": [1,2]}")
        self.assertEqual(_clean_llm_json_output("{\"a\":1, \"b\":[{\"c\":2,}, {\"d\":3,}], \"e\":{\"f\":4,},}"), "{\"a\":1, \"b\":[{\"c\":2}, {\"d\":3}], \"e\":{\"f\":4}}")
        self.assertEqual(_clean_llm_json_output("   {\"key\": \"value\"}   "), "{\"key\": \"value\"}")
        self.assertEqual(_clean_llm_json_output("Not json"), "Not json")
        self.assertEqual(_clean_llm_json_output(""), "")
        self.assertEqual(_clean_llm_json_output("{\"key\": \"value\"} # comment"), "{\"key\": \"value\"} # comment") # Comments are not removed by this simple cleaner

    def test_clean_llm_json_output_repairs_truncated(self):
        self.assertEqual(_clean_llm_json_output('{"a":1'), '{"a":1}')
        self.assertEqual(_clean_llm_json_output('{"a":{"b":[1,2]}'), '{"a":{"b":[1,2]}}')
        self.assertEqual(_clean_llm_json_output('{"a":"val'), '{"a":"val"}')

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_full_extraction(self, mock_robust_chat):
        mock_response_data = {
            "nombre_cliente": "Juan Pérez",
            "telefono_cliente": "1122334455",
            "direccion_cliente": "Calle Falsa 123, Springfield",
            "email_cliente": "juan.perez@example.com"
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "Soy Juan Pérez, mi tel es 1122334455, vivo en Calle Falsa 123, Springfield. Email: juan.perez@example.com"
        potential_fields = ["nombre_cliente", "telefono_cliente", "direccion_cliente", "email_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)

        self.assertEqual(result, mock_response_data)
        mock_robust_chat.assert_called_once()

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_partial_extraction(self, mock_robust_chat):
        mock_response_data = {
            "nombre_cliente": "Ana López",
            "email_cliente": "ana.lopez@example.com"
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "Mi nombre es Ana López y mi correo es ana.lopez@example.com"
        potential_fields = ["nombre_cliente", "telefono_cliente", "email_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)

        self.assertEqual(result, mock_response_data)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_no_details(self, mock_robust_chat):
        mock_robust_chat.return_value = json.dumps({})

        text = "Quisiera hacer un pedido de tortas."
        potential_fields = ["nombre_cliente", "telefono_cliente", "email_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)

        self.assertEqual(result, {})

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_empty_input(self, mock_robust_chat):
        result = extract_multiple_contact_details_llm("", ["nombre_cliente"])
        self.assertEqual(result, {})
        mock_robust_chat.assert_not_called()

        result = extract_multiple_contact_details_llm("Algún texto", [])
        self.assertEqual(result, {})
        mock_robust_chat.assert_not_called()

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_llm_returns_extra_fields(self, mock_robust_chat):
        mock_response_data = {
            "nombre_cliente": "Carlos Ruiz",
            "ciudad": "México DF", # Campo no solicitado
            "email_cliente": "c.ruiz@example.com"
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "Soy Carlos Ruiz, de México DF, mi email es c.ruiz@example.com"
        potential_fields = ["nombre_cliente", "email_cliente"] # Solo esperamos estos
        result = extract_multiple_contact_details_llm(text, potential_fields)

        expected_data = {
            "nombre_cliente": "Carlos Ruiz",
            "email_cliente": "c.ruiz@example.com"
        }
        self.assertEqual(result, expected_data)

    @patch('services.llm_utils.robust_chat')
    def test_extract_multiple_contact_details_llm_json_decode_error(self, mock_robust_chat):
        mock_robust_chat.return_value = "Esto no es un JSON {"
        text = "Datos varios"
        potential_fields = ["nombre_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)
        self.assertEqual(result, {}) # Espera diccionario vacío en caso de error

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_full_extraction(self, mock_robust_chat):
        mock_response_data = {
            "tipo_problema": "Alumbrado público",
            "ubicacion_problema": "Calle Sol 123, frente al parque",
            "descripcion_problema": "La farola no enciende desde hace una semana."
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "Hola, en Calle Sol 123, frente al parque, hay una farola que no enciende desde hace una semana."
        result = extract_complaint_details_llm(text)

        self.assertEqual(result, mock_response_data)
        mock_robust_chat.assert_called_once()

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_partial_extraction(self, mock_robust_chat):
        mock_response_data = {
            "tipo_problema": "Recolección de residuos",
            "descripcion_problema": "No pasaron a recoger la basura hoy."
            # ubicacion_problema podría estar ausente si el LLM no la encuentra
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "No pasaron a recoger la basura hoy."
        result = extract_complaint_details_llm(text)

        # El resultado solo debe contener las claves que el LLM devolvió con valor
        self.assertIn("tipo_problema", result)
        self.assertIn("descripcion_problema", result)
        self.assertNotIn("ubicacion_problema", result) # Asumiendo que el mock no lo devuelve

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_empty_input(self, mock_robust_chat):
        result = extract_complaint_details_llm("")
        self.assertEqual(result, {})
        mock_robust_chat.assert_not_called()

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_llm_returns_empty_values(self, mock_robust_chat):
        mock_response_data = {
            "tipo_problema": "", # LLM podría devolver clave con valor vacío
            "ubicacion_problema": "Plaza Central",
            "descripcion_problema": ""
        }
        mock_robust_chat.return_value = json.dumps(mock_response_data)

        text = "Problema en la Plaza Central."
        result = extract_complaint_details_llm(text)

        expected_data = { # La función filtra claves con valores vacíos
            "ubicacion_problema": "Plaza Central"
        }
        self.assertEqual(result, expected_data)

    @patch('services.llm_utils.robust_chat')
    def test_update_summary_with_llm_extraction_basic_append(self, mock_robust_chat):
        # Test the basic append logic (mocking LLM to simulate it not being the advanced one)
        # To force basic append, we make robust_chat behave like the simple mock defined in llm_utils.py
        # when services.cohere_ai isn't available, or ensure it raises an exception to trigger fallback.

        # Option 1: Simulate the mock from llm_utils.py directly by making robust_chat NOT gpt-4
        # This is tricky because the function checks module. Forcing basic append via exception.
        mock_robust_chat.side_effect = Exception("Simulated LLM failure for summary update")

        current_summary = "Cliente reportó problema."
        extracted_data = {"tipo_problema": "Fuga de agua", "urgencia": "Alta"}

        expected_new_lines = [
            "- Tipo problema: Fuga de agua", # Note: Keys are capitalized and underscores replaced
            "- Urgencia: Alta"
        ]

        result = update_summary_with_llm_extraction(current_summary, extracted_data)

        self.assertIn(current_summary, result)
        for line in expected_new_lines:
            self.assertIn(line, result)

    @patch('services.llm_utils.robust_chat')
    def test_update_summary_with_llm_extraction_empty_initial_summary(self, mock_robust_chat):
        mock_robust_chat.side_effect = Exception("Simulated LLM failure for summary update") # Force basic append

        current_summary = ""
        extracted_data = {"nombre_cliente": "Laura Pausini", "id_pedido": "P123"}

        result = update_summary_with_llm_extraction(current_summary, extracted_data)

        self.assertIn("- Nombre cliente: Laura Pausini", result)
        self.assertIn("- Id pedido: P123", result)
        self.assertFalse(result.startswith("\n")) # Check no leading newline if summary was empty

    @patch('services.llm_utils.robust_chat')
    def test_update_summary_with_llm_extraction_empty_extracted_data(self, mock_robust_chat):
        # This test assumes the intelligent LLM summarizer is NOT used,
        # or if it is, it correctly handles empty new data.
        # Forcing basic append for predictability:
        mock_robust_chat.side_effect = Exception("Simulated LLM failure for summary update")

        current_summary = "Resumen existente."
        extracted_data = {}
        result = update_summary_with_llm_extraction(current_summary, extracted_data)
        self.assertEqual(result, current_summary)

    @patch('services.llm_utils.robust_chat')
    def test_update_summary_with_llm_extraction_intelligent_merge(self, mock_robust_chat):
        # This test is for the case where the "real" robust_chat is used for summarization
        # The llm_utils.py file has a check:
        # `is_mock_chat = hasattr(robust_chat, '__module__') and robust_chat.__module__ == __name__`
        # To test the intelligent merge, we need robust_chat to NOT be the mock from llm_utils.
        # So, we patch it to be a MagicMock from a different "module" or without __module__ attribute.

        # Forcing the 'intelligent merge' path:
        # We need robust_chat to be something that is NOT the mock defined in llm_utils.py
        # The easiest way is to ensure it doesn't have `__module__ == 'services.llm_utils'`
        # (or whatever __name__ is for llm_utils.py when it runs).
        # A simple MagicMock will do, as it won't match the mock's module.
        mock_robust_chat.return_value = "Resumen inteligentemente actualizado con: Tipo de problema - Fuga."
        # Ensure the mock doesn't look like the local mock in llm_utils
        mock_robust_chat.__module__ = 'some.other.module'


        current_summary = "El cliente Juan Pérez reportó un problema."
        extracted_data = {"tipo_problema": "Fuga de agua", "ubicacion_problema": "Baño principal"}

        result = update_summary_with_llm_extraction(current_summary, extracted_data)

        self.assertEqual(result, "Resumen inteligentemente actualizado con: Tipo de problema - Fuga.")

        # Check that the prompt to the LLM was constructed correctly
        args, kwargs = mock_robust_chat.call_args
        self.assertIn("CURRENT SUMMARY: '''El cliente Juan Pérez reportó un problema.'''", args[0])
        self.assertIn("NEW DATA (in JSON format): '''{", args[0])
        self.assertIn("\"tipo_problema\": \"Fuga de agua\"", args[0])
        self.assertIn("\"ubicacion_problema\": \"Baño principal\"", args[0])

    @patch('services.llm_utils.robust_chat')
    def test_extract_complaint_details_llm_with_markdown(self, mock_robust_chat):
        mock_response_str = "```json\n{\"tipo_problema\": \"Basura\", \"descripcion_problema\": \"Mucha basura en la esquina.\"}\n```"
        mock_robust_chat.return_value = mock_response_str

        text = "Hay mucha basura en la esquina de mi casa."
        result = extract_complaint_details_llm(text)

        expected = {
            "tipo_problema": "Basura",
            "descripcion_problema": "Mucha basura en la esquina."
        }
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_contact_details_llm_with_trailing_commas(self, mock_robust_chat):
        mock_response_str = "{\"nombre_cliente\": \"Pedro Navaja\", \"telefono_cliente\": \"555-9876\",}"
        mock_robust_chat.return_value = mock_response_str

        text = "Soy Pedro Navaja, mi teléfono es 555-9876."
        potential_fields = ["nombre_cliente", "telefono_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)

        expected = {
            "nombre_cliente": "Pedro Navaja",
            "telefono_cliente": "555-9876"
        }
        self.assertEqual(result, expected)

    @patch('services.llm_utils.robust_chat')
    def test_extract_contact_details_llm_regex_fallback(self, mock_robust_chat):
        mock_robust_chat.return_value = json.dumps({})

        text = "Hola, soy Ana Gomez. Tel 261-1234567, vivo en Mitre 123. Email ana@test.com"
        potential_fields = ["nombre_cliente", "telefono_cliente", "direccion_cliente", "email_cliente"]
        result = extract_multiple_contact_details_llm(text, potential_fields)

        self.assertEqual(result.get("nombre_cliente"), "Ana Gomez")
        self.assertTrue(result.get("telefono_cliente"))
        self.assertEqual(result.get("email_cliente"), "ana@test.com")
        self.assertTrue(result.get("direccion_cliente"))

if __name__ == '__main__':
    unittest.main(verbosity=2)
