import unittest
from unittest.mock import MagicMock, patch
from services.common_utils import extract_multiple_contact_details_regex
from services.municipio_responder import ReclamoFlowHandler, CONTEXTO_MUNICIPIO

class TestDniExtractionAndConfirmation(unittest.TestCase):
    def test_extracts_dni(self):
        text = "Marcelo Guillen 32877851 guillen.marce@gmail.com 2613168608"
        data = extract_multiple_contact_details_regex(text)
        self.assertEqual(data.get("dni"), "32877851")
        self.assertEqual(data.get("email"), "guillen.marce@gmail.com")

    def test_enumerated_name_phone_city(self):
        text = "1. Juan Perez\n2. +5491112345678\n3. CABA"
        data = extract_multiple_contact_details_regex(text)
        self.assertEqual(data.get("nombre"), "Juan Perez")
        self.assertEqual(data.get("telefono"), "+5491112345678")
        self.assertEqual(data.get("ciudad"), "CABA")

    def test_text_confirmation(self):
        context = {"chat_db_context_data": {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {
            "datos_reclamo": {
                "categoria": "Luminaria",
                "direccion": "Calle 123",
                "descripcion": "poste caido",
                "nombre": "Juan Perez",
                "dni": "12345678",
                "email": "jp@example.com",
                "telefono": "+5400000000"
            },
            "state": "ESPERANDO_CONFIRMACION"
        }}}}
        handler = ReclamoFlowHandler(context, MagicMock())
        with patch('services.municipio_responder.CrearReclamoActionHandler') as mock_handler:
            inst = mock_handler.return_value
            inst.execute.return_value = {"success": True, "message_to_user": "ok", "options_list": []}
            resp = handler.handle_confirmacion("confirmar", {})
            self.assertIn("ok", resp["message_body"])
            inst.execute.assert_called_once()

if __name__ == '__main__':
    unittest.main()
