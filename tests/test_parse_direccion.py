import unittest
from unittest.mock import patch

from services import herramientas_municipio as hm


class TestParseDireccionFallback(unittest.TestCase):
    @patch.object(hm, '_parse_direccion_basica')
    @patch.object(hm, 'openai_client')
    def test_fallback_to_basic_parser(self, mock_openai_client, mock_basic_parser):
        # Simulate OpenAI failure
        mock_openai_client.chat.completions.create.side_effect = Exception('boom')

        mock_basic_parser.return_value = {
            'calle': 'Sarmiento',
            'numero': '100',
            'localidad': 'Mendoza'
        }

        result = hm.parse_direccion_completa('Sarmiento 100, Mendoza')

        self.assertIsNotNone(result)
        self.assertEqual(result['calle'], 'Sarmiento')
        mock_basic_parser.assert_called_once()


if __name__ == '__main__':
    unittest.main()
