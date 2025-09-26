import os
import unittest
from unittest.mock import patch

# Avoid runtime errors from optional third-party clients when running tests
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("CO_API_KEY", "test")
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)

from services.points_of_interest_handler import PointsOfInterestHandler


def _sample_info(**overrides):
    base = {
        "texto": "🅿️ Demo",
        "libres": 1,
        "camera": "Demo",
        "timestamp": "00:00",
        "segmentos": [],
        "resolved_lat": -33.023818,
        "resolved_lon": -68.497164,
        "matched_reference": "Las Heras 105, Junín",
    }
    base.update(overrides)
    return base


class TestParkingPOI(unittest.TestCase):
    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_parking_response_nearest(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertTrue(any("Las Heras 105" in line for line in body_lines))
        self.assertIn("spots", res)
        self.assertGreater(len(res["spots"]), 0)
        self.assertIn("lat", res["spots"][0])

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_parking_response_string_coords(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": "-33.023818", "lon": "-68.497164", "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertTrue(any("Las Heras 105" in line for line in body_lines))

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_parking_zero_latitude_accepted(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": 0, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        mock_occ.assert_called_once()

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_parking_response_geocode_string(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        res = handler.handle({"pregunta": "estacionamiento", "location": "Las Heras 105"})
        body_lines = res.get("message_body", "").splitlines()
        self.assertGreaterEqual(len(body_lines), 2)
        self.assertTrue(any("Las Heras 105" in line for line in body_lines))
        mock_occ.assert_called_once()

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info(libres=3))
    def test_parking_response_availability_free(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        lines = [line for line in res.get("message_body", "").splitlines() if line.startswith("-")]
        self.assertTrue(any("libre" in line for line in lines))
        free_spots = [spot for spot in res.get("spots", []) if spot.get("available_spots", 0) > 0]
        self.assertTrue(free_spots)

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info(libres=0))
    def test_parking_response_handles_no_libres(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        loc = {"lat": -33.023818, "lon": -68.497164, "address": "Las Heras 105, Junín, Mendoza"}
        res = handler.handle({"pregunta": "estacionamiento", "location": loc})
        lines = [line for line in res.get("message_body", "").splitlines() if line.startswith("-")]
        self.assertTrue(lines)
        self.assertTrue(all("ocupado" in line for line in lines))
        self.assertTrue(all(spot.get("available_spots") == 0 for spot in res.get("spots", [])))

    def test_parking_keyword_estacionar(self):
        handler = PointsOfInterestHandler(context={})
        res = handler.handle({"pregunta": "quiero estacionar", "location": None})
        self.assertIn("Compartir ubicación", str(res))

    def test_parking_query_sets_waiting_state(self):
        context = {"chat_db_context_data": {}}
        handler = PointsOfInterestHandler(context=context)
        res = handler.handle({"pregunta": "Buscar estacionamiento libre", "location": None})
        self.assertIn("Compartir ubicación", str(res))
        municipio_ctx = context["chat_db_context_data"]["contexto_municipio_v2"]
        from services.conversation_state import ConversationState

        self.assertEqual(
            municipio_ctx.get("estado_conversacion"),
            ConversationState.ESPERANDO_UBICACION_GENERAL.name,
        )
        self.assertEqual(
            municipio_ctx.get("consulta_pendiente_ubicacion"),
            "Buscar estacionamiento libre",
        )

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_parking_text_address_in_question(self, mock_occ):
        handler = PointsOfInterestHandler(context={})
        res = handler.handle({"pregunta": "estacionamiento Las Heras 105", "location": None})
        mock_occ.assert_called()
        self.assertIn("Las Heras 105", res.get("message_body", ""))

    def test_action_identifier_does_not_trigger_geocode(self):
        context = {"chat_db_context_data": {}}
        handler = PointsOfInterestHandler(context=context)
        with patch("services.points_of_interest_handler.consultar_ocupacion") as mock_occ:
            res = handler.handle({"pregunta": "buscar_estacionamiento", "location": None})
            self.assertIn("ubicación", res.get("message_body", "").lower())
            mock_occ.assert_not_called()

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_stateful_location_triggers_parking(self, mock_occ):
        import eventlet

        eventlet.monkey_patch = lambda *args, **kwargs: None

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

    @patch("services.points_of_interest_handler.consultar_ocupacion", return_value=_sample_info())
    def test_repeat_share_location_uses_last_query(self, mock_occ):
        import eventlet

        eventlet.monkey_patch = lambda *args, **kwargs: None

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
                chat_session_id='test_repeat_share',
                user_id=1,
                context_data={'contexto_municipio_v2': {'estado_conversacion': None, 'ultima_consulta_poi': 'estacionamiento'}}
            )
            db.session.add(ctx)
            db.session.commit()

            # User presses the share location button again
            prompt_resp = responder_municipio(
                pregunta_original={'pregunta': 'Compartir ubicación', 'action': 'compartir_ubicacion'},
                owner_user=owner_user,
                rubro_obj=rubro,
                viewer_user=owner_user,
                chat_db_context=ctx
            )

            self.assertIn('Compartir ubicación', prompt_resp.get('message_body', ''))

