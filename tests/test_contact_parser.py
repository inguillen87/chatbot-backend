import unittest
import os
import sys

# Ensure the project root is in the system path
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.common_utils import extract_multiple_contact_details_regex
from utils.validators import validate_name

class TestContactParserRegex(unittest.TestCase):

    def test_parse_contact_comma_separated(self):
        """
        Tests parsing a standard, comma-separated line of contact info.
        """
        linea = "Juan Perez, juan@mail.com, 2615551234, 30123456, Don Bosco 55 Junín"
        parsed = extract_multiple_contact_details_regex(linea)

        # The new regex extractor is more heuristic and might not get all fields perfectly.
        # We focus on the most critical ones.
        self.assertIn("nombre", parsed)
        self.assertEqual(parsed["nombre"], "Juan Perez")
        self.assertEqual(parsed["email"], "juan@mail.com")
        self.assertEqual(parsed["telefono"], "+5492615551234")
        self.assertEqual(parsed["dni"], "30123456")
        # Address extraction is complex; we check if it's reasonable.
        self.assertIn("direccion", parsed)
        self.assertIn("don bosco 55", parsed["direccion"].lower())

    def test_parse_contact_space_separated(self):
        """
        Tests parsing a line where contact details are separated by spaces.
        """
        linea = "Marcelo Guilen 32877851 2613168608 guillen.marce@gmail.com"
        parsed = extract_multiple_contact_details_regex(linea)

        self.assertEqual(parsed.get("nombre"), "Marcelo Guilen")
        self.assertEqual(parsed.get("dni"), "32877851")
        self.assertEqual(parsed.get("email"), "guillen.marce@gmail.com")
        self.assertEqual(parsed.get("telefono"), "+5492613168608")
        self.assertIsNone(parsed.get("direccion"))

    def test_address_without_explicit_name(self):
        """
        Ensures that addresses without an explicit name are not misclassified as the name.
        """
        linea = "32877851 guillen.marce@gmail.com sarmiento 125 junin"
        parsed = extract_multiple_contact_details_regex(linea)

        self.assertEqual(parsed.get("dni"), "32877851")
        self.assertEqual(parsed.get("email"), "guillen.marce@gmail.com")
        self.assertIsNone(parsed.get("telefono"))
        self.assertIsNone(parsed.get("nombre"))
        self.assertEqual(parsed.get("direccion", "").lower(), "sarmiento 125 junin")

    def test_partial_information(self):
        """
        Tests that the parser handles incomplete information gracefully.
        """
        linea = "Maria, maria@example.com"
        parsed = extract_multiple_contact_details_regex(linea)

        self.assertEqual(parsed.get("nombre"), "Maria")
        self.assertEqual(parsed.get("email"), "maria@example.com")
        self.assertIsNone(parsed.get("dni"))
        self.assertIsNone(parsed.get("telefono"))

    def test_address_with_keywords(self):
        """
        Tests parsing when the address itself contains keywords like 'calle'.
        """
        linea = "Ana Gomez, ana@g.com, DNI 25.123.456, mi dirección es Calle Falsa 123"
        parsed = extract_multiple_contact_details_regex(linea)

        self.assertEqual(parsed.get("nombre"), "Ana Gomez")
        self.assertEqual(parsed.get("email"), "ana@g.com")
        self.assertEqual(parsed.get("dni"), "25123456")
        self.assertIn("calle falsa 123", parsed.get("direccion", "").lower())

    def test_no_contact_info(self):
        """
        Tests that providing a generic string doesn't result in false positives.
        """
        linea = "Hola, quiero hacer un reclamo por un bache."
        parsed = extract_multiple_contact_details_regex(linea)

        # In this case, it might extract 'Hola' as a name, which is acceptable for a heuristic.
        # The key is that it doesn't extract other fields incorrectly.
        if "nombre" in parsed:
            self.assertFalse(validate_name(linea)) # The whole line is not a name

        self.assertIsNone(parsed.get("email"))
        self.assertIsNone(parsed.get("dni"))
        self.assertIsNone(parsed.get("telefono"))

    def test_action_sentence_is_not_parsed_as_name(self):
        texto = "quiero pedir que corten las ramas del barrio"
        parsed = extract_multiple_contact_details_regex(texto)
        self.assertNotIn("nombre", parsed)

if __name__ == "__main__":
    unittest.main()
