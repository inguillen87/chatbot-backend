import io
import unittest
from unittest.mock import patch

import pandas as pd

from services.document_processing_service import DocumentProcessingService


class DocumentProcessingServiceTest(unittest.TestCase):
    def setUp(self):
        self.service = DocumentProcessingService()

    @patch("services.document_processing_service.pd.read_excel")
    @patch("services.document_processing_service.llamar_llm_para_json_estructurado")
    def test_process_excel_detects_header_and_structures_items(self, mock_llm, mock_read_excel):
        mock_llm.return_value = {
            "resumen": "Resumen ejemplo",
            "items": [
                {
                    "nombre": "Malbec 750",
                    "descripcion": "",
                    "unidad": "botella",
                    "cantidad": "1",
                    "precio_unitario": "1250",
                    "moneda": "ARS",
                    "subtotal_estimado": "1250",
                }
            ],
            "totales": {"moneda": "ARS", "total_estimado": "1250"},
            "contacto": {"nombre": "", "telefono": "", "email": ""},
        }

        rows = [
            ["", "", ""],
            ["Nombre Producto", "Precio", "Unidad"],
            ["Malbec 750", "1250", "botella"],
            ["Cabernet 750", "1300", "botella"],
        ]
        df_raw = pd.DataFrame(rows)
        df_processed = pd.DataFrame(
            {
                "Nombre Producto": ["Malbec 750", "Cabernet 750"],
                "Precio": ["1250", "1300"],
                "Unidad": ["botella", "botella"],
            }
        )

        mock_read_excel.side_effect = [df_raw, df_processed]

        result = self.service.process_document(b"fake-bytes", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "catalogo.xlsx")

        self.assertTrue(result["success"])
        self.assertIn("nombre producto", result["texto_extraido"])
        self.assertEqual(result["metadata"]["header_row_index"], 1)
        self.assertEqual(result["datos_estructurados"], mock_llm.return_value)
        mock_llm.assert_called_once()

    @patch("services.document_processing_service.pd.read_csv")
    @patch("services.document_processing_service.pd.read_excel", side_effect=ValueError("Not an excel file"))
    @patch("services.document_processing_service.llamar_llm_para_json_estructurado")
    def test_process_csv_falls_back_to_read_csv(self, mock_llm, mock_read_excel, mock_read_csv):
        mock_llm.return_value = {"resumen": "", "items": [], "totales": {"moneda": "", "total_estimado": ""}, "contacto": {"nombre": "", "telefono": "", "email": ""}}

        csv_rows = [
            ["Producto", "Precio", "Unidad"],
            ["Malbec", "1000", "botella"],
            ["Cabernet", "1200", "botella"],
        ]
        df_raw = pd.DataFrame(csv_rows)
        df_processed = pd.DataFrame(
            {
                "Producto": ["Malbec", "Cabernet"],
                "Precio": ["1000", "1200"],
                "Unidad": ["botella", "botella"],
            }
        )

        mock_read_csv.side_effect = [df_raw, df_processed]

        result = self.service.process_document("Producto,Precio,Unidad\nMalbec,1000,botella\nCabernet,1200,botella".encode("utf-8"), "text/csv", "catalogo.csv")

        self.assertTrue(result["success"])
        self.assertEqual(mock_read_excel.call_count, 1)
        self.assertEqual(mock_read_csv.call_count, 2)
        self.assertEqual(result["metadata"]["formato_fuente"], "csv")
        mock_llm.assert_called_once()

    def test_process_document_with_empty_payload_returns_error(self):
        result = self.service.process_document(b"", "application/pdf", "empty.pdf")

        self.assertFalse(result["success"])
        self.assertIn("Contenido vacío", result["error"])


if __name__ == "__main__":
    unittest.main()
