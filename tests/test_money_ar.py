import unittest
from decimal import Decimal
from utils.money_ar import parse_ars, format_ars

class TestMoneyAr(unittest.TestCase):
    def test_parse_ars(self):
        # Case 1: Standard with thousand separator and decimal comma
        self.assertEqual(parse_ars("15.620,00"), Decimal("15620.00"))

        # Case 2: No decimal part, thousand separator
        self.assertEqual(parse_ars("5.207"), Decimal("5207"))

        # Case 3: Symbol and spaces
        self.assertEqual(parse_ars("$ 2.603,33"), Decimal("2603.33"))

        # Case 4: Weird spacing from PDF extraction
        self.assertEqual(parse_ars("1 5.620,00"), Decimal("15620.00"))

        # Case 5: Simple number
        self.assertEqual(parse_ars("100"), Decimal("100"))

        # Case 6: None
        self.assertEqual(parse_ars(None), Decimal("0"))

    def test_format_ars(self):
        # Case 1: Standard formatting
        val = Decimal("15620.00")
        self.assertEqual(format_ars(val), "15.620,00")

        # Case 2: Rounding
        val = Decimal("5.2066") # Should round up to 5.21
        self.assertEqual(format_ars(val), "5,21") # Wait, format_ars logic handles int/dec split differently in my impl vs requested?
        # The provided code splits by "." which comes from f-string.
        # 5.2066 -> "5.21" -> int="5", dec="21" -> "5,21". Correct.

        # Case 3: No decimals
        val = Decimal("5207")
        self.assertEqual(format_ars(val, decimals=0), "5.207")

        # Case 4: Zero
        val = Decimal("0")
        self.assertEqual(format_ars(val), "0,00")

if __name__ == '__main__':
    unittest.main()
