import unittest
from services.presupuesto_pdf import generar_presupuesto_pdf, FPDF

class PresupuestoPDFTests(unittest.TestCase):
    def test_generar_presupuesto_pdf(self):
        if FPDF is None:
            self.skipTest("fpdf2 not available")
        items = [{"nombre": "Prod", "cantidad": 2, "precio": 10}]
        pdf = generar_presupuesto_pdf(items, {"nombre": "Cliente"})
        self.assertIsInstance(pdf, (bytes, bytearray))
        self.assertGreater(len(pdf), 0)

if __name__ == "__main__":
    unittest.main()
