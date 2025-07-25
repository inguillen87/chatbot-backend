import unittest
from types import SimpleNamespace
from unittest.mock import patch
import os
from services.qdrant_search import armar_respuesta_legible, formatear_tabla_catalogo


class DummyHit(SimpleNamespace):
    pass


class QdrantFormattingTests(unittest.TestCase):
    def test_deduplication_of_products(self):
        hit1 = DummyHit(
            payload={"nombre": "Vino Tinto", "id": "A1", "precio_str": "10"}, score=0.9
        )
        hit2 = DummyHit(
            payload={"nombre": "Vino Tinto", "id": "A1", "precio_str": "10"}, score=0.8
        )
        hit3 = DummyHit(
            payload={"nombre": "Vino Blanco", "id": "B2", "precio_str": "12"},
            score=0.7,
        )
        texto = armar_respuesta_legible([hit1, hit2, hit3], max_items=5)
        self.assertEqual(texto.count("* **Vino"), 2)

    def test_merge_fields_from_duplicate_hits(self):
        hit1 = DummyHit(
            payload={"nombre": "Vino Tinto", "id": "A1", "precio_str": "10"}, score=0.9
        )
        hit2 = DummyHit(
            payload={"nombre": "Vino Tinto", "id": "A1", "descripcion": "Rojo"},
            score=0.8,
        )
        texto = armar_respuesta_legible([hit1, hit2], max_items=5)
        self.assertEqual(texto.count("* **Vino Tinto**"), 1)
        self.assertIn("10", texto)
        self.assertIn("Rojo", texto)

    def test_table_formatting_includes_all_columns(self):
        hit = DummyHit(
            payload={
                "marca": "Vincent",
                "varietal": "Malbec",
                "caja": "6",
                "precio_botella": "$2.732",
                "precio_caja": "$16.390",
                "nombre": "Vino Test",
                "id": "V1"
            },
            score=0.9,
        )
        tabla = formatear_tabla_catalogo([hit])
        self.assertIn("| Marca", tabla)
        self.assertIn("| Varietal", tabla)
        self.assertIn("| Caja", tabla)
        self.assertIn("Vincent", tabla)
        self.assertIn("Malbec", tabla)


if __name__ == "__main__":
    unittest.main()
