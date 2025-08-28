import unittest
from unittest.mock import patch
from services.points_of_interest_handler import PointsOfInterestHandler

class TestParkingPOI(unittest.TestCase):
    def test_parking_response_nearest(self):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertIn("Las Heras 105", body_lines[1])

    def test_parking_response_string_coords(self):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": "-33.023818", "lon": "-68.497164", "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertIn("Las Heras 105", body_lines[1])

    @patch("services.points_of_interest_handler.get_coordinates")
    def test_parking_response_geocode_string(self, mock_geo):
        mock_geo.return_value = {"lat": -33.023818, "lon": -68.497164}
        handler = PointsOfInterestHandler(context={})
        res = handler.handle({"pregunta": "estacionamiento", "location": "Las Heras 105"})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertIn("Las Heras 105", body_lines[1])
        mock_geo.assert_called_once()

    @patch("services.points_of_interest_handler.random.randint", return_value=3)
    def test_parking_response_availability_free(self, mock_rand):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        self.assertIn("3 lugares libres", res.get("message_body", ""))

    @patch("services.points_of_interest_handler.random.randint", return_value=0)
    def test_parking_response_availability_full(self, mock_rand):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        self.assertIn("todo ocupado", res.get("message_body", ""))

    def test_parking_keyword_estacionar(self):
        handler = PointsOfInterestHandler(context={})
        res = handler.handle({"pregunta": "quiero estacionar", "location": None})
        self.assertIn("Compartir ubicación", str(res))
