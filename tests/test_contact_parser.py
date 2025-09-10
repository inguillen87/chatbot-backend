import unittest
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.municipio_responder import _parse_contact_compact_text


class TestContactParser(unittest.TestCase):
    def test_parse_contact_compact_line(self):
        linea = "Juan Perez, juan@mail.com, 2615551234, 30123456, Don Bosco 55 Junín"
        parsed = _parse_contact_compact_text(linea)
        self.assertEqual(parsed["nombre"], "Juan Perez")
        self.assertEqual(parsed["email"], "juan@mail.com")
        self.assertTrue(parsed["telefono"].startswith("+54"))
        self.assertEqual(parsed["dni"], "30123456")
        self.assertEqual(parsed["direccion_contacto"], "Don Bosco 55 Junín")


if __name__ == "__main__":
    unittest.main()
