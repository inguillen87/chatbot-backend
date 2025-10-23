import json
import unittest
from types import SimpleNamespace

from services.pedido_pdf import extraer_items_pedido, generar_pdf_nota_pedido, FPDF


class TestPedidoPDF(unittest.TestCase):
    def test_extraer_items_pedido(self):
        detalles = json.dumps([
            {"nombre": "Malbec", "cantidad": 6, "precio_unitario": 1500, "subtotal": 9000},
            {"producto": "Espumante", "qty": 3, "precio": 2200},
            "texto libre",
        ])
        pedido = SimpleNamespace(detalles=detalles)

        items = extraer_items_pedido(pedido)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["nombre"], "Malbec")
        self.assertAlmostEqual(items[1]["subtotal"], 6600.0)

    def test_generar_pdf_nota_pedido(self):
        if FPDF is None:
            self.skipTest("fpdf2 not available")

        detalles = json.dumps([
            {"nombre": "Malbec", "cantidad": 6, "precio_unitario": 1500},
            {"nombre": "Syrah", "cantidad": 2, "precio_unitario": 1800},
        ])
        pedido = SimpleNamespace(
            detalles=detalles,
            monto_total=0,
            nombre_cliente="Ana",
            email_cliente="ana@example.com",
            telefono_cliente="12345",
            direccion="Ruta 40 km 12",
            nro_pedido="PED-001",
            asunto="Pedido de prueba",
        )

        pdf_bytes = generar_pdf_nota_pedido(
            pedido,
            empresa_info={"nombre": "Cuatro Fincas", "direccion": "Mendoza"},
        )
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

