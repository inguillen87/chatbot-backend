import unittest
from unittest import mock
import os
import sys

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from services.municipio_responder import (
    _parse_contact_compact_text,
    _merge_contact,
    procesar_datos_contacto_compacto,
)


class TestContactParser(unittest.TestCase):
    def test_parse_contact_compact_line(self):
        linea = "Juan Perez, juan@mail.com, 2615551234, 30123456, Don Bosco 55 Junín"
        parsed = _parse_contact_compact_text(linea)
        self.assertEqual(parsed["nombre"], "Juan Perez")
        self.assertEqual(parsed["email"], "juan@mail.com")
        self.assertTrue(parsed["telefono"].startswith("+54"))
        self.assertEqual(parsed["dni"], "30123456")
        self.assertEqual(parsed["direccion_contacto"], "don bosco 55 junín")

    def test_invalid_phone_is_cleared(self):
        linea = "Juan Perez, juan@mail.com, 123, 30123456, Don Bosco 55 Junín"
        with mock.patch("services.municipio_responder.validar_telefono", return_value=False):
            parsed = _parse_contact_compact_text(linea)
        self.assertIsNone(parsed["telefono"])

    def test_merge_overrides_placeholder_name(self):
        base = {"nombre": "Vecino/a", "email": None, "telefono": None, "dni": None, "direccion_contacto": None}
        nuevo = {"nombre": "Marcelo Guillen"}
        merged = _merge_contact(base, nuevo)
        self.assertEqual(merged["nombre"], "Marcelo Guillen")

    def test_procesar_updates_reclamo_address(self):
        texto = (
            "Marcelo Guillen, guillen.marce@gmail.com, 2613168608, 32877851, don bosco 55 esquina sarmiento junin"
        )
        datos = procesar_datos_contacto_compacto(texto, {})
        self.assertEqual(datos["nombre"], "Marcelo Guillen")
        self.assertEqual(datos["direccion_contacto"], "don bosco 55 esquina sarmiento junin")
        self.assertNotIn("direccion_reclamo", datos)


if __name__ == "__main__":
    unittest.main()
