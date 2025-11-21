import unittest

from services.common_utils import parse_precio_flexible


class ParsePrecioFlexibleTests(unittest.TestCase):
    def test_parse_localized_price_with_symbol_and_separators(self):
        precio_str, precio_float, moneda = parse_precio_flexible("$ 1.250,50.-")
        self.assertEqual(precio_str, "1250.5")
        self.assertAlmostEqual(precio_float, 1250.5)
        self.assertEqual(moneda, "ARS")

    def test_parse_price_with_euro_text(self):
        precio_str, precio_float, moneda = parse_precio_flexible("EUR 2,345.99")
        self.assertEqual(precio_str, "2345.99")
        self.assertAlmostEqual(precio_float, 2345.99)
        self.assertEqual(moneda, "EUR")

    def test_parse_numeric_string_without_currency(self):
        precio_str, precio_float, moneda = parse_precio_flexible("800.0")
        self.assertEqual(precio_str, "800")
        self.assertAlmostEqual(precio_float, 800.0)
        self.assertIsNone(moneda)

    def test_non_numeric_input_returns_none_float(self):
        precio_str, precio_float, moneda = parse_precio_flexible("No es un precio")
        self.assertEqual(precio_str, "No es un precio")
        self.assertIsNone(precio_float)
        self.assertIsNone(moneda)

    def test_accepts_non_string_input(self):
        precio_str, precio_float, moneda = parse_precio_flexible(1234)
        self.assertEqual(precio_str, "1234")
        self.assertAlmostEqual(precio_float, 1234.0)
        self.assertIsNone(moneda)


if __name__ == "__main__":
    unittest.main()
