import types
from unittest.mock import patch
import unittest
import services.herramientas_municipio as hm


class TestObtenerDireccionDeCoordenadas(unittest.TestCase):
    @patch('services.herramientas_municipio._reverse_geocode_with_geopy')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    @patch('services.herramientas_municipio.geocodificar_inversa_llm')
    def test_openai_success(self, mock_openai, mock_parse, mock_geopy):
        mock_openai.return_value = {'formatted_address': 'Sarmiento 100, Mendoza'}
        mock_parse.return_value = {
            'calle': 'Sarmiento',
            'numero': '100',
            'localidad': 'Mendoza'
        }
        result = hm.obtener_direccion_de_coordenadas(-32.89, -68.83)
        assert result['formatted_address'] == 'Sarmiento 100, Mendoza'
        mock_parse.assert_called_once_with('Sarmiento 100, Mendoza')
        mock_geopy.assert_not_called()

    @patch('services.herramientas_municipio.requests.get')
    @patch('services.herramientas_municipio._reverse_geocode_with_geopy')
    @patch('services.herramientas_municipio.parse_direccion_completa')
    @patch('services.herramientas_municipio.geocodificar_inversa_llm')
    def test_geopy_fallback(self, mock_openai, mock_parse, mock_geopy, mock_requests_get):
        mock_openai.return_value = None
        mock_geopy.return_value = {
            'calle': 'San Martin',
            'numero': '123',
            'localidad': 'Mendoza',
            'formatted_address': 'San Martin 123, Mendoza'
        }
        result = hm.obtener_direccion_de_coordenadas(-1, -1)
        assert result['calle'] == 'San Martin'
        mock_geopy.assert_called_once()
        mock_requests_get.assert_not_called()
