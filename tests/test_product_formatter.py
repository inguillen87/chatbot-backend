import unittest
from services.product_formatter import format_product_list


class ProductFormatterTests(unittest.TestCase):
    def test_basic_formatting(self):
        productos = [
            {
                "nombre": "Malbec Reserva",
                "bodega": "Cuatro Fincas",
                "precio_str": "12000",
                "presentacion": "Botella 750ml",
                "variedad": "Malbec",
                "stock": 8,
                "moneda": "ARS",
            },
            {
                "nombre": "Campera Deportiva",
                "marca": "Nike",
                "talle": "L",
                "color": "Negro",
                "precio_str": "35000",
                "moneda": "ARS",
            },
        ]
        texto = format_product_list(productos)
        self.assertIn("Malbec Reserva", texto)
        self.assertIn("Cuatro Fincas", texto)
        self.assertIn("Variedad", texto)
        self.assertIn("Campera Deportiva", texto)
        self.assertIn("Nike", texto)

    def test_variant_grouping(self):
        productos = [
            {
                "nombre": "Campera Deportiva",
                "marca": "Nike",
                "talle": "L",
                "color": "Negro",
                "precio_str": "35000",
            },
            {
                "nombre": "Campera Deportiva",
                "marca": "Nike",
                "talle": "M",
                "color": "Rojo",
                "precio_str": "35000",
            },
        ]
        texto = format_product_list(productos)
        self.assertEqual(texto.count("Campera Deportiva"), 1)
        self.assertIn("L, M", texto)
        self.assertIn("Negro, Rojo", texto)


if __name__ == "__main__":
    unittest.main()
