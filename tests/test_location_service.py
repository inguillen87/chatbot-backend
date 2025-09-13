import unittest
from unittest.mock import patch

class TestLocationService(unittest.TestCase):
    @patch("services.location_service.GOOGLE_MAPS_API_KEY", "test")
    @patch("services.location_service.googlemaps.Client")
    def test_geocode_address_restricts_country(self, mock_client, *_):
        from services import location_service as ls
        mock_instance = mock_client.return_value
        mock_instance.geocode.return_value = [
            {"geometry": {"location": {"lat": -32.89, "lng": -68.83}}}
        ]
        result = ls.geocode_address("San Martin")
        mock_instance.geocode.assert_called_once_with(
            "San Martin",
            region="ar",
            components={"country": "AR"},
        )
        self.assertEqual(result["geometry"]["location"]["lat"], -32.89)

    @patch("services.location_service.GOOGLE_MAPS_API_KEY", "test")
    @patch("services.location_service.googlemaps.Client")
    def test_autocomplete_address_uses_country(self, mock_client, *_):
        from services import location_service as ls
        mock_instance = mock_client.return_value
        mock_instance.places_autocomplete.return_value = [
            {"description": "San Martin, Mendoza, Argentina"}
        ]
        result = ls.autocomplete_address("San Martin")
        mock_instance.places_autocomplete.assert_called_once_with(
            input_text="San Martin",
            language="es",
            components={"country": "ar"},
        )
        self.assertEqual(result[0]["description"], "San Martin, Mendoza, Argentina")

if __name__ == "__main__":
    unittest.main()
