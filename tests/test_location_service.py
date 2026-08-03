import os
import unittest
from unittest.mock import patch

class TestLocationService(unittest.TestCase):
    @patch.dict(
        os.environ,
        {
            "MUNICIPIO_ID": "default",
            "GOOGLE_MAPS_ALLOW_NETWORK_IN_TESTS": "1",
        },
        clear=False,
    )
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
            "San Martin, Junín, Mendoza",
            region="ar",
            components={
                "locality": "Junín",
                "administrative_area": "Mendoza",
                "country": "AR",
            },
            bounds={
                "southwest": {"lat": -33.2, "lng": -68.6},
                "northeast": {"lat": -32.9, "lng": -68.3},
            },
        )
        self.assertEqual(result["geometry"]["location"]["lat"], -32.89)

    @patch.dict(
        os.environ,
        {
            "MUNICIPIO_ID": "default",
            "GOOGLE_MAPS_ALLOW_NETWORK_IN_TESTS": "1",
        },
        clear=False,
    )
    @patch("services.location_service.GOOGLE_MAPS_API_KEY", "test")
    @patch("services.location_service.googlemaps.Client")
    def test_autocomplete_address_uses_country(self, mock_client, *_):
        from services import location_service as ls
        mock_instance = mock_client.return_value
        mock_instance.places_autocomplete.return_value = [
            {"description": "San Martin, Mendoza, Argentina"}
        ]
        result = ls.autocomplete_address("San Martin")
        mock_instance.places_autocomplete.assert_called_once()
        kwargs = mock_instance.places_autocomplete.call_args.kwargs
        self.assertEqual(kwargs["input_text"], "San Martin")
        self.assertEqual(kwargs["language"], "es")
        self.assertEqual(
            kwargs["components"],
            {
                "administrative_area": "Mendoza",
                "country": "AR",
            },
        )
        self.assertAlmostEqual(kwargs["location"]["lat"], -33.05, places=4)
        self.assertAlmostEqual(kwargs["location"]["lng"], -68.45, places=4)
        self.assertEqual(kwargs["radius"], 16650)
        self.assertEqual(result[0]["description"], "San Martin, Mendoza, Argentina")

if __name__ == "__main__":
    unittest.main()
