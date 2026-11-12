"""Real PYME delivery helpers, with catalogue/network effects replaced."""

import unittest
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

from services.pyme_multimodal import (
    PymeSessionState,
    add_items_to_cart,
    detect_intent_from_text,
    handle_keyword_intent,
    handle_location_payload,
)


class PymeContextFirstDeliveryTests(unittest.TestCase):
    CONFIG = {"ubicaciones": [{"coordenadas": {"lat": -34.6, "lon": -58.4}}]}
    LOCATION = {"latitude": -34.61, "longitude": -58.41, "address": "Calle de prueba 123"}

    def setUp(self):
        catalog = patch("services.pyme_multimodal.load_catalog", return_value=[])
        catalog.start()
        self.addCleanup(catalog.stop)
        network = patch("socket.socket.connect", side_effect=AssertionError("Network is forbidden in this test"))
        network.start()
        self.addCleanup(network.stop)

    def _scope(self, **changes):
        scope = {"owner_id": 17, "tenant_id": 23, "chat_session_id": "session-test-a", "user_id": None, "anon_id": "anon-test-a"}
        scope.update(changes)
        return scope

    def _state_with_cart(self, backing=None, scope=None):
        state = PymeSessionState(backing if backing is not None else {}, 17, session_scope=scope or self._scope())
        add_items_to_cart(state, [({"sku": "TEST-1", "nombre": "Producto de prueba", "precio": 100}, 2)])
        return state

    def _delivery_turn(self, state, *, intent=None, context=None, owner_id=17):
        current_context = {"tenant_id": 23, "config": self.CONFIG}
        current_context.update(context or {})
        return handle_keyword_intent(
            intent=intent or detect_intent_from_text("delivery"), text="delivery", state=state,
            owner_user_id=owner_id, rubro_slug="general", context=current_context, channel="whatsapp",
        )

    def _located_state(self, backing=None, scope=None):
        state = self._state_with_cart(backing, scope)
        handle_location_payload(state, self.CONFIG, self.LOCATION)
        return state

    def _assert_asks_location(self, result):
        self.assertEqual(result.source, "pyme_delivery_solicitud_ubicacion")
        self.assertNotIn("delivery", result.data)

    def test_delivery_reuses_location_already_given_for_current_cart(self):
        state = self._state_with_cart()
        original = handle_location_payload(state, self.CONFIG, self.LOCATION)
        result = self._delivery_turn(state)
        self.assertEqual(result.source, "pyme_delivery_estimate")
        self.assertEqual(result.data["delivery"], original.data["delivery"])
        self.assertNotIn("compartís la ubicación", result.message_body)

    def test_current_delivery_survives_regular_json_session_roundtrip(self):
        backing = {}
        self._located_state(backing).save()
        state = PymeSessionState(json.loads(json.dumps(backing)), 17, session_scope=self._scope())
        self.assertEqual(self._delivery_turn(state).source, "pyme_delivery_estimate")

    def test_authenticated_current_actor_can_reuse_own_location(self):
        state = self._located_state(scope=self._scope(user_id=31, anon_id=None))
        result = self._delivery_turn(state, context={"viewer_user_id": 31})
        self.assertEqual(result.source, "pyme_delivery_estimate")
        self._assert_asks_location(self._delivery_turn(state, context={"viewer_user_id": 32}))

    def test_scope_changes_cannot_reuse_or_retain_bound_location(self):
        for changes, owner_id in [
            ({"chat_session_id": "session-test-b"}, 17),
            ({"tenant_id": 24}, 17),
            ({"anon_id": "anon-test-b"}, 17),
            ({"user_id": 31, "anon_id": None}, 17),
            ({"owner_id": 18}, 18),
        ]:
            with self.subTest(changed=next(iter(changes))):
                backing = {}
                self._located_state(backing).save()
                state = PymeSessionState(backing, owner_id, session_scope=self._scope(**changes))
                self.assertEqual(state.delivery, {})
                self._assert_asks_location(self._delivery_turn(state, owner_id=owner_id))

    def test_current_handler_owner_and_tenant_must_match_binding(self):
        state = self._located_state()
        self._assert_asks_location(self._delivery_turn(state, owner_id=18))
        self._assert_asks_location(self._delivery_turn(state, context={"tenant_id": 24}))

    def test_legacy_unbound_location_is_not_silently_reused(self):
        state = PymeSessionState({}, 17)
        handle_location_payload(state, self.CONFIG, self.LOCATION)
        self._assert_asks_location(self._delivery_turn(state))

    def test_incomplete_or_ambiguous_identity_does_not_bind_location(self):
        for changes in [
            {"chat_session_id": ""}, {"tenant_id": None}, {"owner_id": 18},
            {"anon_id": None}, {"user_id": 31}, {"user_id": True, "anon_id": None},
        ]:
            with self.subTest(invalid=next(iter(changes))):
                state = self._located_state(scope=self._scope(**changes))
                self.assertNotIn("delivery_scope", state.raw)
                self._assert_asks_location(self._delivery_turn(state))

    def test_editing_current_cart_keeps_location_and_recalculates_current_tariff(self):
        state = self._located_state()
        prior_shipping = state.delivery["shipping_total"]
        add_items_to_cart(state, [({"sku": "TEST-2", "nombre": "Otro producto", "precio": 200}, 3)])
        config = {**self.CONFIG, "tarifa_base_envio": 5000}
        result = self._delivery_turn(state, context={"config": config})
        self.assertEqual(result.source, "pyme_delivery_estimate")
        self.assertEqual(state.delivery["address"], self.LOCATION["address"])
        self.assertGreater(state.delivery["shipping_total"], prior_shipping)
        self.assertEqual(len(state.cart["items"]), 2)

    def test_completed_order_then_new_cart_does_not_inherit_location(self):
        state = self._located_state()
        with patch("services.pyme_multimodal.persist_order", return_value=SimpleNamespace(id=44, nro_pedido="P-TEST", consulta_pin=None)):
            result = self._delivery_turn(state, intent="confirmar")
        self.assertEqual(result.source, "pyme_pedido_registrado")
        self.assertEqual(state.delivery, {})
        self.assertNotIn("delivery_scope", state.raw)
        add_items_to_cart(state, [({"sku": "TEST-3", "nombre": "Pedido nuevo", "precio": 300}, 1)])
        self._assert_asks_location(self._delivery_turn(state))

    def test_normal_cancel_or_new_order_context_reset_does_not_inherit_location(self):
        backing = {}
        self._located_state(backing).save()
        backing.clear()  # The existing orchestration resets the PYME context.
        state = self._state_with_cart(backing)
        self._assert_asks_location(self._delivery_turn(state))

    def test_explicit_address_edit_waits_for_valid_replacement(self):
        state = self._located_state()
        self._assert_asks_location(self._delivery_turn(state, context={"selected_action_id": "editar_direccion"}))
        self._assert_asks_location(self._delivery_turn(state))
        with patch("services.pyme_multimodal.persist_order") as persist:
            self._assert_asks_location(self._delivery_turn(state, intent="confirmar"))
            persist.assert_not_called()
        replacement = {"lat": -34.62, "lng": -58.42, "address": "Otra dirección de prueba"}
        handle_location_payload(state, self.CONFIG, replacement)
        result = self._delivery_turn(state)
        self.assertEqual(result.source, "pyme_delivery_estimate")
        self.assertEqual(result.data["delivery"]["address"], replacement["address"])
        self.assertEqual(result.data["delivery"]["lat"], replacement["lat"])

    def test_invalid_location_never_produces_an_estimate_or_binds_stale_point(self):
        for invalid in [
            {"lat": 91, "lng": 1}, {"lat": 1, "lng": 181},
            {"lat": "nan", "lng": 1}, {"lat": 1, "lng": "inf"},
            {"lat": True, "lng": 1}, {"address": "Sólo texto"}, None,
        ]:
            with self.subTest(invalid=invalid):
                state = self._located_state()
                self._assert_asks_location(handle_location_payload(state, self.CONFIG, invalid))
                self._assert_asks_location(self._delivery_turn(state))

    def test_corrupt_persisted_coordinates_are_not_reused(self):
        state = self._located_state()
        state.delivery["lng"] = "not-a-coordinate"
        self._assert_asks_location(self._delivery_turn(state))

    def test_zero_coordinates_and_numeric_strings_are_valid_without_address_invention(self):
        state = self._state_with_cart()
        config = {"ubicaciones": [{"coordenadas": {"lat": 0, "lng": 0}}]}
        result = handle_location_payload(state, config, {"latitude": "0", "longitude": "0"})
        self.assertEqual(result.source, "pyme_delivery_estimate")
        self.assertEqual(state.delivery["shipping_total"], 1800)
        self.assertEqual(state.delivery["lat"], 0)
        self.assertEqual(state.delivery["lng"], 0)
        self.assertIsNone(state.delivery["address"])

    @contextmanager
    def _orm_consumer(self, *, authenticated=False):
        from flask import Flask
        from models import ChatSessionContext, Rubro, TenantProfile, User, db
        from services.pymes import CONTEXTO_PYME, responder_pyme

        app = Flask(__name__)
        app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite://", SQLALCHEMY_TRACK_MODIFICATIONS=False)
        db.init_app(app)
        with app.app_context():
            db.create_all()
            try:
                rubro = Rubro(clave="delivery-test", nombre="general")
                owner = User(name="Tienda de prueba", email="delivery-owner@example.test", password_hash="test-only", rol="admin", rubro=rubro)
                viewer = User(name="Cliente de prueba", email="delivery-viewer@example.test", password_hash="test-only", rol="cliente")
                other_viewer = User(name="Otro cliente de prueba", email="delivery-other@example.test", password_hash="test-only", rol="cliente")
                db.session.add_all([owner, viewer, other_viewer])
                db.session.flush()
                tenant = TenantProfile(slug="delivery-test", nombre="Tienda de prueba", tipo="pyme", pyme_id=owner.id)
                db.session.add(tenant)
                db.session.flush()
                backing = {}
                draft = PymeSessionState(backing, owner.id)
                add_items_to_cart(draft, [({"sku": "ORM-TEST", "nombre": "Producto de prueba", "precio": 100}, 2)])
                draft.save()
                session = ChatSessionContext(
                    chat_session_id="00000000-0000-4000-8000-000000000017", tenant_id=tenant.id,
                    user_id=viewer.id if authenticated else owner.id,
                    anon_id=None if authenticated else "anon-consumer-test", context_data={CONTEXTO_PYME: backing},
                )
                db.session.add(session)
                db.session.commit()
                session_id = session.chat_session_id

                def turn(payload, *, different_identity=False):
                    current = db.session.get(ChatSessionContext, session_id)
                    current_viewer = (other_viewer if different_identity else viewer) if authenticated else None
                    current_anon = None if authenticated else ("retired-anon-test" if different_identity else "anon-consumer-test")
                    with app.test_request_context("/ask/pyme", method="POST"):
                        result = responder_pyme(
                            payload, owner_user=owner, rubro_obj=rubro, chat_db_context=current,
                            viewer_user=current_viewer, anon_id=current_anon, channel="whatsapp", tenant_id=tenant.id,
                        )
                    db.session.commit()
                    db.session.expire_all()
                    return result

                def config(_rubro, filename, **_kwargs):
                    return self.CONFIG if filename == "config.json" else []

                with patch("services.pymes.cargar_configuracion_pyme", side_effect=config), patch(
                    "services.pymes.llamar_llm_con_fallback", side_effect=AssertionError("LLM is not part of this delivery path")
                ):
                    yield turn
            finally:
                db.session.remove()
                db.drop_all()

    def test_responder_pyme_builds_scope_and_reuses_location_after_orm_roundtrip(self):
        with self._orm_consumer() as turn:
            first = turn({"pregunta": "", "ubicacion_usuario": dict(self.LOCATION)})
            self.assertEqual(first["fuente"], "pyme_delivery_estimate")
            reused = turn("delivery")
            self.assertEqual(reused["fuente"], "pyme_delivery_estimate")
            self.assertEqual(reused["data"]["delivery"]["address"], self.LOCATION["address"])
            self.assertEqual(reused["data"]["delivery"]["lat"], self.LOCATION["latitude"])

    def test_responder_pyme_edit_action_is_reachable_and_replaces_location(self):
        with self._orm_consumer() as turn:
            turn({"pregunta": "", "ubicacion_usuario": dict(self.LOCATION)})
            edit = turn({"pregunta": "", "action_id": "editar_direccion"})
            self.assertEqual(edit["fuente"], "pyme_delivery_solicitud_ubicacion")
            still_waiting = turn("delivery")
            self.assertEqual(still_waiting["fuente"], "pyme_delivery_solicitud_ubicacion")
            replacement = {"latitude": -34.62, "longitude": -58.42, "address": "Otra dirección de prueba"}
            updated = turn({"pregunta": "", "ubicacion_usuario": replacement})
            self.assertEqual(updated["fuente"], "pyme_delivery_estimate")
            reused = turn("delivery")
            self.assertEqual(reused["fuente"], "pyme_delivery_estimate")
            self.assertEqual(reused["data"]["delivery"]["lat"], replacement["latitude"])
            self.assertEqual(reused["data"]["delivery"]["address"], replacement["address"])

    def test_responder_pyme_does_not_reuse_location_for_retired_anonymous_or_authenticated_actor(self):
        for authenticated in (False, True):
            with self.subTest(authenticated=authenticated), self._orm_consumer(authenticated=authenticated) as turn:
                first = turn({"pregunta": "", "ubicacion_usuario": dict(self.LOCATION)})
                self.assertEqual(first["fuente"], "pyme_delivery_estimate")
                changed_actor = turn("delivery", different_identity=True)
                self.assertEqual(changed_actor["fuente"], "pyme_delivery_solicitud_ubicacion")


if __name__ == "__main__":
    unittest.main()
