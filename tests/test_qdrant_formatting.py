import unittest
from types import SimpleNamespace
from unittest.mock import patch
import os
from services.qdrant_search import armar_respuesta_legible

class DummyHit(SimpleNamespace):
    pass

class QdrantFormattingTests(unittest.TestCase):
    def test_deduplication_of_products(self):
        hit1 = DummyHit(payload={"nombre": "Vino Tinto", "sku": "A1", "precio_str": "10"}, score=0.9)
        hit2 = DummyHit(payload={"nombre": "Vino Tinto", "sku": "A1", "precio_str": "10"}, score=0.8)
        hit3 = DummyHit(payload={"nombre": "Vino Blanco", "sku": "B2", "precio_str": "12"}, score=0.7)
        texto = armar_respuesta_legible([hit1, hit2, hit3], max_items=5)
        # Solo deben aparecer dos productos en el listado
        self.assertEqual(texto.count("**Vino"), 2)

    def test_links_use_base_url(self):
        hit = DummyHit(payload={"id": 5, "nombre": "Tornillos", "precio_str": "100", "cantidad": "10"}, score=0.9)
        with patch('config.Config.TIENDA_BASE_URL', 'https://tienda.example.com'):
            texto = armar_respuesta_legible([hit], max_items=1)
        self.assertIn("https://tienda.example.com/carrito/agregar/5", texto)

if __name__ == '__main__':
    unittest.main()
