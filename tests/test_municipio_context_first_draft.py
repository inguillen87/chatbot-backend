"""Draft assembly only: original handlers/ORM, synthetic prior interpretation/media.

Call prepare_process in an isolated process before import. No provider/media
interpretation or outer multi-vertical routing acceptance is implied.
"""
from copy import deepcopy
import unittest

from tests.test_municipio_explicit_claim_offline import ExplicitClaimOfflineHttpTest
from services import municipio_responder as municipal
from services.constants import CONTEXTO_MUNICIPIO
from models import CategoriaTicket, MunicipioTicket, TenantProfile, TenantTicket, db


class ContextFirstDraftTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ExplicitClaimOfflineHttpTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.category("Arbolado")
        self.fixture.category("Luminaria")
        self.session = self.fixture.session()
        self.data = self.session.context_data
        self.municipal_ctx = self.data.setdefault(CONTEXTO_MUNICIPIO, {})
        self.context = {
            "user_obj": self.fixture.fixture.mock_client_user, "viewer_user_obj": None,
            "tenant_profile": self.fixture.tenant, "tenant_id": self.fixture.tenant.id,
            "anon_id": self.session.anon_id, "chat_db_context_data": self.data,
            "chat_db_context_obj": self.session, "municipio_config_actual": {}, "channel": "whatsapp",
        }

    def tearDown(self):
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(self.fixture.provider_calls, 0)
        self.fixture.complaint_extractor.assert_not_called()

    def prior(self, **extra):
        self.municipal_ctx["datos_parciales_llm_reclamo"] = {
            "categoria": "Arbolado", "descripcion": "Arbol caido",
            "direccion": "Calle de Prueba 123", "coordenadas": {"lat": -33.14, "lon": -68.49},
            **extra,
        }

    def flow(self):
        return self.municipal_ctx["reclamo_flow_v2"]

    def start(self, initial=None, category=None):
        return municipal.ReclamoFlowHandler(self.context, self.session).start_flow(initial, category)

    def test_explicit_text_reuses_same_session_structured_address(self):
        self.prior()
        municipal._try_start_explicit_reclamo_from_text("quiero hacer un reclamo por un arbol caido", self.context, self.session)
        fields = self.flow()["datos_reclamo"]
        self.assertEqual(fields["direccion"], "Calle de Prueba 123")
        self.assertEqual(fields["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
        self.assertNotIn("datos_parciales_llm_reclamo", self.municipal_ctx)

    def test_already_transcribed_audio_is_text_without_another_provider_call(self):
        self.prior()
        transcript = "quiero hacer un reclamo por un arbol caido"
        self.context["whatsapp_inbound_content"] = {"type": "audio", "transcription_available": True}
        self.context["user_input_raw"] = transcript
        municipal._try_start_explicit_reclamo_from_text(transcript, self.context, self.session)
        self.assertEqual(self.flow()["datos_reclamo"]["descripcion"], transcript)
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Calle de Prueba 123")

    def test_caption_and_structured_gps_keep_current_evidence_without_address_question(self):
        self.context.update(es_foto=True, es_ubicacion=True, foto_url="https://example.invalid/evidence.webp", ubicacion_usuario={"latitude": -33.14, "longitude": -68.49, "address": "Calle de Prueba 123"})
        self.start({"categoria": "Arbolado", "descripcion": "Arbol caido"})
        fields = self.flow()["datos_reclamo"]
        self.assertEqual(fields["direccion"], "Calle de Prueba 123")
        self.assertEqual(fields["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertEqual(fields["foto_url"], "https://example.invalid/evidence.webp")
        self.assertIn(self.flow()["state"], {"ESPERANDO_DATOS_CONTACTO", "ESPERANDO_CONFIRMACION"})

    def test_conflicting_prior_and_current_address_requests_only_location(self):
        self.prior()
        response = self.start({"descripcion": "Arbol caido", "direccion": "Otra Calle 456"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
        self.assertIn("dos ubicaciones", response["message_body"])
        self.assertEqual(self.flow()["datos_reclamo"]["categoria"], "Arbolado")
        self.assertNotIn("coordenadas", self.flow()["datos_reclamo"])

    def test_same_turn_conflicting_explicit_address_and_gps_do_not_mix(self):
        self.context.update(es_ubicacion=True, ubicacion_usuario={"latitude": -33.14, "longitude": -68.49, "address": "Otra Calle 456"})
        response = self.start({"categoria": "Arbolado", "descripcion": "Arbol caido", "direccion": "Calle de Prueba 123"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
        self.assertIn("dos ubicaciones", response["message_body"])
        self.assertNotIn("direccion", self.flow()["datos_reclamo"])
        self.assertNotIn("coordenadas", self.flow()["datos_reclamo"])

    def test_conflicting_categories_request_category_without_losing_address(self):
        self.prior()
        response = self.start({"categoria": "Luminaria", "descripcion": "Luz apagada"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_CATEGORIA")
        self.assertIn("dos categorías", response["message_body"])
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Calle de Prueba 123")

    def test_partial_category_must_belong_to_current_tenant_catalog(self):
        other = TenantProfile(slug="context-other", nombre="Otro", tipo="municipio", municipio_id=self.context["user_obj"].id, is_active=True)
        db.session.add(other)
        db.session.flush()
        db.session.add(CategoriaTicket(tenant_id=other.id, nombre="Categoria ajena", tipo="ticket"))
        db.session.flush()
        self.prior(categoria="Categoria ajena")
        self.start()
        self.assertNotIn("categoria", self.flow()["datos_reclamo"])
        self.assertEqual(self.flow()["state"], "ESPERANDO_CATEGORIA")

    def test_scope_mismatch_does_not_reuse_partial_address_or_media(self):
        for kind in ("tenant", "anon", "detached_context", "inactive", "owner"):
            with self.subTest(kind=kind):
                self.prior(foto_url="https://example.invalid/old.webp")
                previous = dict(self.context)
                active = self.fixture.tenant.is_active
                owner = self.fixture.tenant.municipio_id
                if kind == "tenant": self.context["tenant_id"] = self.fixture.tenant.id + 1000
                if kind == "anon": self.context["anon_id"] = "another-synthetic-session"
                if kind == "detached_context": self.context["chat_db_context_data"] = deepcopy(self.data)
                if kind == "inactive": self.fixture.tenant.is_active = False
                if kind == "owner": self.fixture.tenant.municipio_id = owner + 1000
                handler = municipal.ReclamoFlowHandler(self.context, self.session)
                handler.start_flow({"categoria": "Arbolado", "descripcion": "Arbol caido"})
                fields = handler.flow_context["datos_reclamo"]
                self.assertNotIn("direccion", fields)
                self.assertNotIn("foto_url", fields)
                self.context = previous
                self.fixture.tenant.is_active = active
                self.fixture.tenant.municipio_id = owner

    def test_missing_interpretation_does_not_invent_image_description(self):
        self.context.update(es_foto=False, foto_url="https://example.invalid/stale.webp", datos_interpretados_archivo={"categoria_sugerida": "Arbolado", "descripcion_sugerida": "Unseen synthetic content"})
        self.start({"categoria": "Arbolado"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_DESCRIPCION")
        self.assertNotIn("descripcion", self.flow()["datos_reclamo"])
        self.assertNotIn("foto_url", self.flow()["datos_reclamo"])

    def test_invalid_gps_does_not_become_coordinates_or_address(self):
        for location in ({"latitude": 999, "longitude": 10}, {"latitude": float("nan"), "longitude": 10}, {"latitude": "bad", "longitude": 10}):
            with self.subTest(location=location):
                self.context.update(es_ubicacion=True, ubicacion_usuario=location)
                self.start({"categoria": "Arbolado", "descripcion": "Arbol caido"})
                self.assertNotIn("coordenadas", self.flow()["datos_reclamo"])
                self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_cancelled_flow_clears_partials_before_a_later_draft(self):
        self.prior()
        handler = municipal.ReclamoFlowHandler(self.context, self.session)
        handler.end_flow("Cancelado")
        self.assertNotIn("datos_parciales_llm_reclamo", self.municipal_ctx)
        self.start({"categoria": "Arbolado", "descripcion": "Otro reclamo"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_generic_explicit_command_does_not_replace_specific_prior_description(self):
        self.prior(descripcion="Una rama grande bloquea el acceso al centro de salud")
        municipal._try_start_explicit_reclamo_from_text("quiero hacer un reclamo", self.context, self.session)
        self.assertEqual(self.flow()["datos_reclamo"]["descripcion"], "Una rama grande bloquea el acceso al centro de salud")

    def test_new_caption_preserves_specific_facts_already_in_the_draft(self):
        self.prior(descripcion="Una rama bloquea la puerta del centro de salud")
        municipal._try_start_explicit_reclamo_from_text("quiero hacer un reclamo por un arbol caido", self.context, self.session)
        description = self.flow()["datos_reclamo"]["descripcion"]
        self.assertIn("bloquea la puerta del centro de salud", description)
        self.assertIn("arbol caido", description)

    def test_precise_gps_with_no_address_is_not_a_fabricated_street(self):
        self.context.update(es_ubicacion=True, ubicacion_usuario={"latitude": -33.14, "longitude": -68.49})
        self.start({"categoria": "Arbolado", "descripcion": "Arbol caido"})
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Ubicación compartida")
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_authenticated_session_reuses_context_only_for_its_actual_actor(self):
        actor = self.fixture.fixture.mock_client_user
        self.session.anon_id = None
        self.session.user_id = actor.id
        self.context.update(anon_id=None, viewer_user_obj=actor)
        self.prior()
        self.start({"descripcion": "Arbol caido"})
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Calle de Prueba 123")
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_authenticated_session_does_not_reuse_context_for_a_different_actor(self):
        self.session.anon_id = None
        self.session.user_id = self.fixture.fixture.mock_client_user.id + 1000
        self.context.update(anon_id=None, viewer_user_obj=self.fixture.fixture.mock_client_user)
        self.prior(foto_url="https://example.invalid/old.webp")
        self.start({"categoria": "Arbolado", "descripcion": "Arbol caido"})
        self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
        self.assertNotIn("foto_url", self.flow()["datos_reclamo"])

    def test_invalid_partial_category_does_not_block_valid_current_category(self):
        self.prior(categoria="Categoria inexistente")
        self.start({"categoria": "Arbolado", "descripcion": "Arbol caido"})
        self.assertEqual(self.flow()["datos_reclamo"]["categoria"], "Arbolado")
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_CATEGORIA")

    def test_nonstring_category_inputs_are_discarded_before_merge_or_prefill(self):
        for source in ("partial", "incoming", "category_argument", "image_interpretation"):
            for value in (["Arbolado"], {"nombre": "Arbolado"}, 123):
                with self.subTest(source=source, kind=type(value).__name__):
                    self.municipal_ctx.pop("datos_parciales_llm_reclamo", None)
                    self.context.pop("es_foto", None)
                    self.context.pop("foto_url", None)
                    self.context.pop("datos_interpretados_archivo", None)
                    initial, category = {"descripcion": "Arbol caido"}, None
                    if source == "partial":
                        self.prior(categoria=value)
                    elif source == "incoming":
                        self.prior()
                        initial["categoria"] = value
                    elif source == "category_argument":
                        category = value
                    else:
                        self.context.update(es_foto=True, foto_url="https://example.invalid/current.webp",
                                            datos_interpretados_archivo={"categoria_sugerida": value})
                    self.start(initial, category)
                    if source == "incoming":
                        self.assertEqual(self.flow()["datos_reclamo"]["categoria"], "Arbolado")
                    else:
                        self.assertNotIn("categoria", self.flow()["datos_reclamo"])
                        self.assertEqual(self.flow()["state"], "ESPERANDO_CATEGORIA")

    def test_coordinate_aliases_of_the_same_point_do_not_create_a_conflict(self):
        self.prior(coordenadas={"lat": -33.14, "lng": -68.49})
        self.start({"coordenadas": {"lat": -33.14, "lon": -68.49}})
        self.assertEqual(self.flow()["datos_reclamo"]["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Calle de Prueba 123")
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_same_gps_without_new_label_preserves_existing_readable_address(self):
        self.prior()
        self.context.update(es_ubicacion=True, ubicacion_usuario={"latitude": -33.14, "longitude": -68.49})
        self.start()
        self.assertEqual(self.flow()["datos_reclamo"]["direccion"], "Calle de Prueba 123")
        self.assertEqual(self.flow()["datos_reclamo"]["coordenadas"], {"lat": -33.14, "lon": -68.49})
        self.assertNotEqual(self.flow()["state"], "ESPERANDO_DIRECCION")

    def test_separate_incomplete_locations_do_not_create_an_unverified_pair(self):
        for kind in ("prior_coordinates_only", "prior_address_only"):
            with self.subTest(kind=kind):
                if kind == "prior_coordinates_only":
                    self.prior(direccion=None)
                    incoming = {"direccion": "Otra Calle 456"}
                else:
                    self.prior(coordenadas=None)
                    self.context.update(es_ubicacion=True, ubicacion_usuario={"latitude": -33.15, "longitude": -68.50})
                    incoming = {}
                response = self.start(incoming)
                self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
                self.assertIn("dos ubicaciones", response["message_body"])
                self.assertNotIn("coordenadas", self.flow()["datos_reclamo"])
                self.assertNotIn("direccion", self.flow()["datos_reclamo"])

    def test_image_interpretation_cannot_bypass_category_or_location_conflict(self):
        for kind in ("category", "location"):
            with self.subTest(kind=kind):
                self.prior()
                self.context.update(
                    es_foto=True, foto_url="https://example.invalid/current.webp",
                    datos_interpretados_archivo={"categoria_sugerida": "Arbolado", "direccion_sugerida": "Calle de Prueba 123"},
                )
                incoming = {"categoria": "Luminaria"} if kind == "category" else {"direccion": "Otra Calle 456"}
                self.start(incoming)
                if kind == "category":
                    self.assertEqual(self.flow()["state"], "ESPERANDO_CATEGORIA")
                    self.assertNotIn("categoria", self.flow()["datos_reclamo"])
                else:
                    self.assertEqual(self.flow()["state"], "ESPERANDO_DIRECCION")
                    self.assertNotIn("direccion", self.flow()["datos_reclamo"])
                self.assertEqual(self.flow()["datos_reclamo"]["foto_url"], "https://example.invalid/current.webp")
