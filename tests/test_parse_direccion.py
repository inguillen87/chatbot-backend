import unittest
from unittest.mock import MagicMock, patch

from services import herramientas_municipio as hm

class TestParseDireccionFallback(unittest.TestCase):
    @patch.object(hm, 'cohere_client')
    @patch.object(hm, 'openai_client')
    def test_fallback_to_cohere(self, mock_openai_client, mock_cohere_client):
        # Simulate OpenAI failure
        mock_openai_client.chat.completions.create.side_effect = Exception('boom')
        # Simulate Cohere success
        cohere_resp = MagicMock()
        cohere_resp.generations = [MagicMock(text='{"calle":"Sarmiento","numero":"100","localidad":"Mendoza"}')]
        mock_cohere_client.generate.return_value = cohere_resp

        result = hm.parse_direccion_completa('Sarmiento 100, Mendoza')
        self.assertIsNotNone(result)
        self.assertEqual(result['calle'], 'Sarmiento')
        mock_cohere_client.generate.assert_called_once()

    @patch.object(hm, 'parse_direccion_completa')
    @patch.object(hm, 'geocode_address')
    def test_direccion_es_valida_llm_fallback(self, mock_geo, mock_parse):
        mock_geo.side_effect = [None, {"lat": -33.0, "lng": -68.0}]
        mock_parse.return_value = {
            "calle": "Sarmiento",
            "numero": "100",
            "localidad": "Junin",
            "provincia": "Mendoza",
        }
        self.assertTrue(hm.direccion_es_valida("Sarmiento 100"))
        self.assertEqual(mock_geo.call_count, 2)
        mock_parse.assert_called_once()

if __name__ == '__main__':
    unittest.main()
