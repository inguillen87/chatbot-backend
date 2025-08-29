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

if __name__ == '__main__':
    unittest.main()
