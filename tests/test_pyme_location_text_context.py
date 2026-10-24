"""Original PYME consumer and catalog/JSON session ORM, no network or AI."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from flask import Flask
from models import CatalogoItem, ChatSessionContext, PymePedido, Rubro, TenantProfile, User, db
from services.pymes import CONTEXTO_PYME, responder_pyme
from services.pyme_multimodal import PymeSessionState


class PymeLocationTextContextTests(unittest.TestCase):
    LOCATION = {"latitude": -34.61, "longitude": -58.41, "address": "Calle de prueba 123"}
    TEXT = "quiero 2 cajas de Malbec"

    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://",
                               SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(self.app)
        self.app_context = self.app.app_context()
        self.app_context.push()
        self.addCleanup(self.app_context.pop)
        db.create_all()
        self.addCleanup(self._remove_database)
        self.rubro = Rubro(clave="location-text-test", nombre="general")
        self.owner = User(name="Tienda de prueba", email="location-owner@example.test",
                          password_hash="test-only", rol="admin", tipo_chat="pyme", rubro=self.rubro)
        self.viewer = User(name="Cliente de prueba", email="location-viewer@example.test",
                           password_hash="test-only", rol="cliente")
        db.session.add_all([self.owner, self.viewer])
        db.session.flush()
        self.tenant = TenantProfile(slug="location-text-test", nombre="Tienda de prueba", tipo="pyme",
                                    pyme_id=self.owner.id, is_active=True)
        db.session.add(self.tenant)
        db.session.flush()
        db.session.add(CatalogoItem(user_id=self.owner.id, tenant_id=self.tenant.id,
            nombre="Malbec de prueba", sku="MAL-TEST", precio="100", cantidad="caja",
            modalidad="venta", disponible=True))
        self.session = ChatSessionContext(chat_session_id="00000000-0000-4000-8000-000000000029",
            tenant_id=self.tenant.id, user_id=self.owner.id, anon_id="anon-location-text", context_data={})
        db.session.add(self.session)
        db.session.commit()
        self.config = {"ubicaciones": [{"coordenadas": {"lat": -34.6, "lon": -58.4}}]}
        def config(_rubro, filename, **_kwargs):
            return self.config if filename == "config.json" else []
        patches = [patch("services.pymes.cargar_configuracion_pyme", side_effect=config),
                   patch("services.pymes.llamar_llm_con_fallback", side_effect=AssertionError("Unexpected AI request")),
                   patch("services.pymes.sugerir_productos_relacionados", return_value=None),
                   patch("socket.socket.connect", side_effect=AssertionError("Network forbidden"))]
        for boundary in patches:
            mocked = boundary.start()
            self.addCleanup(boundary.stop)
            if boundary.attribute == "llamar_llm_con_fallback":
                self.llm = mocked

    def _remove_database(self):
        db.session.remove()
        db.drop_all()

    def turn(self, payload, *, actor=None, anon_id=None):
        with self.app.test_request_context("/ask/pyme", method="POST"):
            response = responder_pyme(payload, owner_user=self.owner, rubro_obj=self.rubro,
                chat_db_context=self.session, viewer_user=actor,
                anon_id=anon_id if anon_id is not None else self.session.anon_id,
                channel="whatsapp" if self.session.anon_id is not None else "web",
                tenant_id=self.tenant.id)
        db.session.commit()
        db.session.expire_all()
        self.assertEqual(PymePedido.query.count(), 0)
        return response

    def state(self):
        return self.session.context_data[CONTEXTO_PYME][PymeSessionState.STORAGE_KEY]

    def test_product_text_and_gps_in_one_turn_preserve_both_in_draft(self):
        response = self.turn({"pregunta": self.TEXT, "ubicacion_usuario": deepcopy(self.LOCATION)})
        self.assertEqual(response["fuente"], "pyme_item_agregado")
        state = self.state()
        self.assertEqual([(item["sku"], item["qty"]) for item in state["cart"]["items"]], [("MAL-TEST", 2)])
        self.assertEqual(state["delivery"]["address"], self.LOCATION["address"])
        self.assertEqual(state["delivery"]["lat"], self.LOCATION["latitude"])
        self.llm.assert_not_called()

    def test_text_only_control_still_adds_catalog_item_without_creating_order(self):
        response = self.turn(self.TEXT)
        self.assertEqual(response["fuente"], "pyme_item_agregado")
        self.assertEqual(self.state()["cart"]["items"][0]["qty"], 2)
        self.llm.assert_not_called()

    def test_location_only_control_still_returns_delivery_estimate(self):
        response = self.turn({"pregunta": "", "ubicacion_usuario": deepcopy(self.LOCATION)})
        self.assertEqual(response["fuente"], "pyme_delivery_estimate")
        self.assertEqual(self.state()["cart"]["items"], [])
        self.assertEqual(self.state()["delivery"]["address"], self.LOCATION["address"])
        self.llm.assert_not_called()

    def test_saved_whatsapp_contact_does_not_change_canonical_anonymous_scope(self):
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": deepcopy(self.LOCATION)}, actor=self.viewer)
        scope = self.state()["delivery_scope"]
        self.assertEqual(scope["anon_id"], self.session.anon_id)
        self.assertIsNone(scope["user_id"])
        self.assertEqual(scope["owner_id"], self.owner.id)
        reused = self.turn("delivery", actor=self.viewer)
        self.assertEqual(reused["fuente"], "pyme_delivery_estimate")

    def test_authenticated_widget_keeps_actual_actor_scope(self):
        self.session.user_id = self.viewer.id
        self.session.anon_id = None
        db.session.commit()
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": deepcopy(self.LOCATION)}, actor=self.viewer)
        scope = self.state()["delivery_scope"]
        self.assertEqual(scope["user_id"], self.viewer.id)
        self.assertIsNone(scope["anon_id"])
        self.assertEqual(self.state()["cart"]["items"][0]["qty"], 2)

    def test_wrong_anonymous_actor_tenant_or_authenticated_actor_cannot_add_same_turn_items(self):
        for kind in ("anon", "tenant", "authenticated_actor"):
            with self.subTest(kind=kind):
                self.session.tenant_id = self.tenant.id
                self.session.anon_id = "anon-location-text"
                self.session.user_id = self.owner.id
                self.session.context_data = {}
                db.session.commit()
                kwargs = {}
                if kind == "anon":
                    kwargs["anon_id"] = "another-anonymous-caller"
                elif kind == "tenant":
                    other_owner = User(name="Otra tienda", email="other-location-owner@example.test",
                                       password_hash="test-only", rol="admin", tipo_chat="pyme")
                    db.session.add(other_owner)
                    db.session.flush()
                    other = TenantProfile(slug="other-location-tenant", nombre="Otra tienda", tipo="pyme",
                                          pyme_id=other_owner.id, is_active=True)
                    db.session.add(other)
                    db.session.flush()
                    self.session.tenant_id = other.id
                else:
                    self.session.anon_id = None
                    self.session.user_id = self.owner.id
                    kwargs["actor"] = self.viewer
                db.session.commit()
                response = self.turn({"pregunta": self.TEXT, "ubicacion_usuario": deepcopy(self.LOCATION)}, **kwargs)
                self.assertEqual(response["fuente"], "pyme_location_text_scope_invalid")
                self.assertFalse(response["success"])
                self.assertEqual(self.state()["cart"]["items"], [])
                self.assertNotIn("delivery_scope", self.state())
                self.llm.assert_not_called()

    def test_zero_coordinates_remain_valid_with_product_text(self):
        self.config["ubicaciones"][0]["coordenadas"] = {"lat": 0, "lon": 0}
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": {"latitude": 0, "longitude": 0}})
        self.assertEqual(self.state()["delivery"]["lat"], 0)
        self.assertEqual(self.state()["delivery"]["lng"], 0)
        self.assertEqual(self.state()["cart"]["items"][0]["qty"], 2)

    def test_invalid_gps_does_not_replace_text_or_create_valid_coordinates(self):
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": {"latitude": 999, "longitude": -58.41}})
        state = self.state()
        self.assertEqual(state["cart"]["items"][0]["qty"], 2)
        self.assertTrue(state["delivery_location_pending"])
        self.assertEqual(state["delivery"], {})
        response = self.turn("confirmar")
        self.assertEqual(response["fuente"], "pyme_delivery_solicitud_ubicacion")

    def test_new_draft_after_context_reset_does_not_inherit_previous_cart_or_location(self):
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": deepcopy(self.LOCATION)})
        self.session.context_data[CONTEXTO_PYME] = {}
        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(self.session, "context_data")
        db.session.commit()
        replacement = {"latitude": -34.62, "longitude": -58.42, "address": "Otra calle de prueba"}
        self.turn({"pregunta": self.TEXT, "ubicacion_usuario": replacement})
        self.assertEqual(self.state()["cart"]["items"][0]["qty"], 2)
        self.assertEqual(self.state()["delivery"]["address"], replacement["address"])

    def test_free_text_and_supplied_gps_reach_existing_structured_llm_boundary(self):
        text = "Para la reunión del viernes"
        self.llm.side_effect = None
        self.llm.return_value = ({"accion_backend": "responder_directamente",
            "datos_estructura": {"target": "pyme"}, "message_body": "¿Qué producto necesitás?"}, {})
        self.turn({"pregunta": text, "ubicacion_usuario": deepcopy(self.LOCATION)})
        self.llm.assert_called_once()
        call = self.llm.call_args.kwargs
        self.assertIn(text, json.loads(call["mensaje_usuario"])["texto"])
        self.assertEqual(call["usuario"]["ubicacion_compartida"], self.LOCATION)
        self.assertEqual(self.state()["cart"]["items"], [])


class PymeLocationTextHttpTests(unittest.TestCase):
    def setUp(self):
        from tests import test_whatsapp_webhook as webhook_tests
        from tests.test_municipio_explicit_claim_offline import _ImmediateTimer
        for setting, value in (("SESSION_TYPE", "null"), ("RATELIMIT_STORAGE_URI", "memory://")):
            config_patch = patch.object(webhook_tests.TestConfig, setting, value)
            config_patch.start()
            self.addCleanup(config_patch.stop)
        self.fixture = webhook_tests.WhatsAppWebhookTestCase("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.fixture._set_owner_tipo_chat("pyme")
        self.tenant = self.fixture._attach_tenant_to_owner(slug="location-text-http", tipo="pyme")
        self.fixture._create_confirmed_session()
        row = self.session()
        row.tenant_id = self.tenant.id
        db.session.add(CatalogoItem(user_id=self.fixture.mock_client_user.id, tenant_id=self.tenant.id,
            nombre="Malbec de prueba", sku="MAL-HTTP", precio="100", cantidad="caja",
            modalidad="venta", disponible=True))
        db.session.commit()
        self.fixture.app.config.update(WELCOME_TEMPLATE_SID=None, WELCOME_MEDIA_URL=None,
            WELCOME_AUDIO_URL=None, WHATSAPP_MENU_AUDIO_ENABLED=False,
            WHATSAPP_AUDIO_ENABLED=False, WELCOME_MESSAGE_DELAY_SECONDS=0)
        self.calls = []
        def send(**kwargs):
            self.calls.append(dict(kwargs))
            return SimpleNamespace(sid="SM" + str(len(self.calls)).zfill(32), status="sent")
        self.fixture.mock_twilio_create.side_effect = send
        def config(_rubro, filename, **_kwargs):
            return {"ubicaciones": [{"coordenadas": {"lat": -34.6, "lon": -58.4}}]} if filename == "config.json" else []
        for boundary in (
            patch("routes.whatsapp_webhook.threading.Timer", _ImmediateTimer),
            patch("routes.whatsapp_webhook._prepare_cached_welcome_audio", return_value=None),
            patch("routes.whatsapp_webhook._ensure_welcome_audio_payload", return_value=None),
            patch("routes.whatsapp_webhook.geocodificar_inversa_llm", return_value=None),
            patch("services.pymes.cargar_configuracion_pyme", side_effect=config),
            patch("services.pymes.llamar_llm_con_fallback", side_effect=AssertionError("Unexpected AI request")),
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def session(self):
        return ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).one()

    def post(self, text):
        response = self.fixture.client.post("/webhook/whatsapp", data={
            "To": "whatsapp:" + self.fixture.test_whatsapp_number_str,
            "From": "whatsapp:" + self.fixture.test_user_number_str,
            "MessageSid": "SM_LOCATION_TEXT_HTTP", "Body": text,
            "Latitude": "-34.61", "Longitude": "-58.41", "Address": "Calle de prueba 123",
            "ProfileName": "Persona de prueba",
        }, headers={"X-Twilio-Signature": "synthetic-fixture-signature"})
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        self.assertEqual(PymePedido.query.count(), 0)
        return self.session().context_data[CONTEXTO_PYME][PymeSessionState.STORAGE_KEY]

    def test_real_webhook_preserves_product_caption_and_gps(self):
        state = self.post(PymeLocationTextContextTests.TEXT)
        self.assertEqual([(item["sku"], item["qty"]) for item in state["cart"]["items"]], [("MAL-HTTP", 2)])
        self.assertEqual(state["delivery"]["address"], "Calle de prueba 123")

    def test_real_whatsapp_anonymous_session_has_reusable_delivery_scope(self):
        state = self.post("")
        self.assertEqual(state["delivery"]["lat"], -34.61)
        self.assertEqual(state["delivery_scope"]["anon_id"], self.fixture.test_user_number_str)
        self.assertEqual(state["delivery_scope"]["owner_id"], self.fixture.mock_client_user.id)
