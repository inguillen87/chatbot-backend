import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch


class TestGoogleMapsServiceFallback(unittest.TestCase):
    @patch.dict(os.environ, {"MAPTILER_API_KEY": "test"}, clear=True)
    @patch("services.google_maps_service.GoogleV3")
    @patch("services.google_maps_service.MapTiler")
    def test_get_coordinates_uses_maptiler_when_google_missing(self, mock_maptiler, mock_google):
        mock_instance = mock_maptiler.return_value
        mock_instance.geocode.return_value = SimpleNamespace(latitude=-33.0, longitude=-68.0)

        from services import google_maps_service as gms
        coords = gms.get_coordinates("Las Heras 105, Junín")

        mock_maptiler.assert_called_once()
        mock_google.assert_not_called()
        self.assertEqual(coords, {"lat": -33.0, "lon": -68.0})

    @patch.dict(os.environ, {}, clear=True)
    @patch("services.google_maps_service.Nominatim")
    def test_get_coordinates_without_api_keys_uses_nominatim(self, mock_nominatim):
        mock_instance = mock_nominatim.return_value
        mock_instance.geocode.return_value = SimpleNamespace(latitude=-33.0, longitude=-68.0)

        from services import google_maps_service as gms
        coords = gms.get_coordinates("Las Heras 105, Junín")

        mock_nominatim.assert_called_once()
        self.assertEqual(coords, {"lat": -33.0, "lon": -68.0})


if __name__ == '__main__':
    unittest.main()
