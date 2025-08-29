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

    def test_stateful_location_triggers_parking(self):
        from app import create_app, db
        from config import TestConfig
        from models import User, Rubro, ChatSessionContext
        from services.municipio_responder import responder_municipio, ConversationState

        app = create_app(TestConfig)
        with app.app_context():
            db.create_all()
            rubro = Rubro(id=1, clave='municipios', nombre='municipios')
            owner_user = User(id=1, tipo_chat='municipio', rol='admin', email='admin@test.com', name='Admin', rubro=rubro)
            owner_user.set_password('pass')
            db.session.add_all([rubro, owner_user])
            db.session.commit()

            ctx = ChatSessionContext(
                chat_session_id='test_parking_state',
                user_id=1,
                context_data={'contexto_municipio_v2': {
                    'estado_conversacion': ConversationState.ESPERANDO_UBICACION_GENERAL.name,
                    'consulta_pendiente_ubicacion': 'estacionamiento'
                }}
            )
            db.session.add(ctx)
            db.session.commit()

            location_payload = {
                'pregunta': '',
                'es_ubicacion': True,
                'ubicacion_usuario': {
                    'latitude': -33.023818,
                    'longitude': -68.497164,
                    'address': 'Las Heras 105, Junín, Mendoza'
                }
            }

            resp = responder_municipio(
                pregunta_original=location_payload,
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=owner_user,
                chat_db_context=ctx,
                location=location_payload['ubicacion_usuario']
            )

            self.assertIn('Las Heras 105', resp.get('message_body', ''))
