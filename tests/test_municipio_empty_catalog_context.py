"""Real municipal HTTP/ORM; identities/signature/provider SDK are synthetic."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.orm import Query

from models import CategoriaTicket, MunicipioTicket, TenantProfile, TenantTicket, db
from services import municipio_responder as municipal
from tests import test_municipio_explicit_claim_offline as explicit_fixture


class EmptyCatalogContextHTTPTests(unittest.TestCase):
    TEXT = "Quiero hacer un reclamo por un árbol caído"

    def setUp(self):
        self.fixture = explicit_fixture.ExplicitClaimOfflineHttpTest("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def tearDown(self):
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(self.fixture.provider_calls, 0)

    def assert_address_after_hello(self):
        before, _ = self.fixture.post("Hola")
        self.assertEqual(before["estado_conversacion"], "ESPERANDO_SELECCION_MENU_PRINCIPAL")
        context, body = self.fixture.post(self.TEXT)
        self.fixture.assert_address_draft(context, self.TEXT)
        self.assertNotIn("Elegí una opción para tu reclamo", body)
        self.fixture.complaint_extractor.assert_not_called()
        return context, body

    def test_empty_catalog_uses_same_published_category_as_manual_path(self):
        context, _ = self.assert_address_after_hello()
        authority = context["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["source"], "published_institutional_menu")
        self.assertEqual(authority["tenant_id"], self.fixture.tenant.id)
        self.assertEqual(authority["name"], "Arbolado")
        self.assertIsNone(authority["category_id"])

    def test_exact_tenant_ticket_catalog_keeps_current_direct_path(self):
        self.fixture.category("Arbolado")
        context, _ = self.assert_address_after_hello()
        authority = context["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["source"], "tenant_ticket_catalog")
        self.assertEqual(authority["category_id"], CategoriaTicket.query.one().id)

    def handler(self):
        session = self.fixture.session()
        return municipal.ReclamoFlowHandler({
            "user_obj": self.fixture.fixture.mock_client_user,
            "tenant_profile": self.fixture.tenant,
            "tenant_id": self.fixture.tenant.id,
            "anon_id": session.anon_id,
            "chat_db_context_data": session.context_data,
        }, session)

    def assert_unresolved_after_hello(self):
        self.fixture.post("Hola")
        context, _ = self.fixture.post(self.TEXT)
        flow = context["reclamo_flow_v2"]
        self.assertEqual(flow["state"], "ESPERANDO_CATEGORIA")
        self.assertEqual(flow["datos_reclamo"]["descripcion"], self.TEXT)
        self.assertNotIn("categoria", flow["datos_reclamo"])
        self.assertNotIn("category_authority", flow)
        return flow

    def test_manual_empty_catalog_uses_same_authority_without_inventing_id(self):
        self.fixture.post("Hola")
        self.fixture.post("1")
        self.fixture.post("1")
        context, _ = self.fixture.post("2")
        flow = context["reclamo_flow_v2"]
        self.assertEqual(flow["datos_reclamo"]["categoria"], "Arbolado")
        self.assertEqual(flow["state"], "ESPERANDO_DIRECCION")
        self.assertEqual(flow["category_authority"]["source"], "published_institutional_menu")
        self.assertIsNone(flow["category_authority"]["category_id"])

    def test_manual_category_alias_uses_same_catalog_authority_as_contextual_text(self):
        self.fixture.category("Árboles")
        self.fixture.post("Hola")
        self.fixture.post("1")
        self.fixture.post("1")
        context, _ = self.fixture.post("2")
        flow = context["reclamo_flow_v2"]
        self.assertEqual(flow["datos_reclamo"].get("categoria"), "Árboles")
        authority = flow.get("category_authority", {})
        self.assertEqual(authority.get("source"), "tenant_ticket_catalog")
        self.assertEqual(authority.get("name"), "Árboles")
        self.assertEqual(authority.get("category_id"), CategoriaTicket.query.one().id)

    def test_contextual_category_alias_preserves_exact_authorized_catalog_label(self):
        self.fixture.category("Árboles")
        self.fixture.post("Hola")
        context, _ = self.fixture.post(self.TEXT)
        self.fixture.assert_address_draft(context, self.TEXT, "Árboles")
        authority = context["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["source"], "tenant_ticket_catalog")
        self.assertEqual(authority["category_id"], CategoriaTicket.query.one().id)

    def test_information_only_catalog_does_not_enable_fallback(self):
        self.fixture.category("Arbolado", kind="informacion")
        self.assert_unresolved_after_hello()

    def test_nonempty_ticket_catalog_does_not_fallback_to_missing_menu_label(self):
        self.fixture.category("Luminaria")
        self.assert_unresolved_after_hello()
        context, _ = self.fixture.post("2")
        self.assertNotIn("categoria", context["reclamo_flow_v2"]["datos_reclamo"])

    def test_compound_catalog_label_is_not_semantically_invented(self):
        self.fixture.category("Arbolado y espacios verdes")
        self.assert_unresolved_after_hello()

    def test_two_authorized_synonym_rows_remain_ambiguous(self):
        self.fixture.category("Arbolado")
        self.fixture.category("Árboles")
        self.assert_unresolved_after_hello()

    def test_foreign_catalog_is_not_inherited_by_current_empty_tenant(self):
        other = TenantProfile(slug="other-catalog-fixture", nombre="Otra institución",
            tipo="municipio", municipio_id=self.fixture.fixture.mock_client_user.id)
        db.session.add(other)
        db.session.commit()
        self.fixture.category("Arbolado", other)
        context, _ = self.assert_address_after_hello()
        authority = context["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["tenant_id"], self.fixture.tenant.id)
        self.assertEqual(authority["source"], "published_institutional_menu")
        self.assertIsNone(authority["category_id"])

    def test_scope_actor_tenant_owner_and_active_bindings_reject_menu_authority(self):
        self.assert_address_after_hello()
        handler = self.handler()
        self.assertIsNotNone(handler._resolve_current_category("Arbolado"))
        mutations = [
            (handler.context, "tenant_id", self.fixture.tenant.id + 999),
            (handler.context, "anon_id", "another-anonymous-actor"),
        ]
        for target, key, value in mutations:
            with self.subTest(binding=key):
                old = target[key]
                target[key] = value
                self.assertIsNone(handler._resolve_current_category("Arbolado"))
                target[key] = old
        with self.subTest(binding="active"):
            self.fixture.tenant.is_active = False
            self.assertIsNone(handler._resolve_current_category("Arbolado"))
            self.fixture.tenant.is_active = True
        with self.subTest(binding="owner"):
            old = self.fixture.tenant.municipio_id
            self.fixture.tenant.municipio_id = old + 999
            self.assertIsNone(handler._resolve_current_category("Arbolado"))
            self.fixture.tenant.municipio_id = old

    def test_optional_SQL_failure_is_not_treated_as_empty_catalog(self):
        self.fixture.post("Hola")
        original_all = Query.all
        failures = []
        def failed_catalog_read(query):
            if any(item.get("entity") is CategoriaTicket for item in query.column_descriptions):
                failures.append(True)
                db.session.execute(text("SELECT * FROM missing_claim_category_authority_fixture"))
            return original_all(query)
        with patch.object(Query, "all", failed_catalog_read), patch.object(db.session, "rollback", wraps=db.session.rollback) as rollback:
            context, _ = self.fixture.post(self.TEXT)
            rollback.assert_not_called()
        flow = context["reclamo_flow_v2"]
        self.assertEqual(len(failures), 1)
        self.assertTrue(db.session.is_active)
        self.assertEqual(flow["state"], "ESPERANDO_CATEGORIA")
        self.assertEqual(flow["datos_reclamo"]["descripcion"], self.TEXT)
        self.assertNotIn("categoria", flow["datos_reclamo"])

    def test_duplicate_inbound_keeps_same_empty_catalog_draft(self):
        context, _ = self.assert_address_after_hello()
        before = deepcopy(context["reclamo_flow_v2"])
        calls = len(self.fixture.calls)
        context, _ = self.fixture.post(self.TEXT, same_sid=True)
        self.assertEqual(context["reclamo_flow_v2"], before)
        self.assertEqual(len(self.fixture.calls), calls)

    def test_ambiguous_user_issues_still_request_category(self):
        self.fixture.post("Hola")
        context, _ = self.fixture.post("quiero hacer un reclamo por un arbol caido y una luminaria rota")
        self.assertNotIn("categoria", context["reclamo_flow_v2"]["datos_reclamo"])
        self.assertEqual(context["reclamo_flow_v2"]["state"], "ESPERANDO_CATEGORIA")

    def test_confirmation_revalidates_changed_catalog_and_keeps_draft_identity(self):
        context, _ = self.assert_address_after_hello()
        before = deepcopy(context["reclamo_flow_v2"])
        self.fixture.category("Luminaria")
        handler = self.handler()
        with patch.object(municipal.CrearReclamoActionHandler, "execute") as create:
            response = handler.handle_confirmacion("confirmar", {})
            create.assert_not_called()
        self.assertIn("antes de registrar", response["message_body"])
        self.assertEqual(handler.flow_context["confirmation_id"], before["confirmation_id"])
        self.assertEqual(handler.flow_context["datos_reclamo"], before["datos_reclamo"])
        self.assertEqual(handler.flow_context["state"], "ESPERANDO_CATEGORIA")

    def test_confirmation_revalidates_current_catalog_revision_even_if_label_remains(self):
        self.fixture.category("Arbolado")
        context, _ = self.assert_address_after_hello()
        before = deepcopy(context["reclamo_flow_v2"])
        self.fixture.category("Luminaria")
        handler = self.handler()
        with patch.object(municipal.CrearReclamoActionHandler, "execute") as create:
            handler.handle_confirmacion("confirmar", {})
            create.assert_not_called()
        self.assertEqual(handler.flow_context["confirmation_id"], before["confirmation_id"])
        self.assertEqual(handler.flow_context["datos_reclamo"], before["datos_reclamo"])

    def test_current_empty_menu_authority_passes_guard_without_formal_test_claim(self):
        context, _ = self.assert_address_after_hello()
        handler = self.handler()
        with patch.object(municipal.CrearReclamoActionHandler, "execute", return_value={"success": False, "message_body": "Synthetic action refused; no persistence"}) as create:
            handler.handle_confirmacion("confirmar", {})
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[0]["categoria"], "Arbolado")
        self.assertEqual(create.call_args.args[0]["descripcion"], self.TEXT)
        self.assertEqual(create.call_args.args[0]["claim_confirmation_id"], context["reclamo_flow_v2"]["confirmation_id"])

    def test_HTTP_reselection_after_revision_change_keeps_same_draft_and_confirmation(self):
        context, _ = self.assert_address_after_hello()
        identity = context["reclamo_flow_v2"]["confirmation_id"]
        session = self.fixture.session()
        data = deepcopy(session.context_data)
        municipal_context = data[municipal.CONTEXTO_MUNICIPIO]
        municipal_context["reclamo_flow_v2"]["state"] = "ESPERANDO_CONFIRMACION"
        municipal_context["estado_conversacion"] = "EN_FLUJO_RECLAMO"
        session.context_data = data
        db.session.commit()
        self.fixture.category("Arbolado")
        self.fixture.category("Luminaria")
        with patch.object(municipal.CrearReclamoActionHandler, "execute") as create:
            context, _ = self.fixture.post("confirmar")
            create.assert_not_called()
        self.assertEqual(context["reclamo_flow_v2"]["confirmation_id"], identity)
        context, _ = self.fixture.post("Luminaria")
        flow = context["reclamo_flow_v2"]
        self.assertEqual(flow["confirmation_id"], identity)
        self.assertEqual(flow["datos_reclamo"]["categoria"], "Luminaria")
        self.assertEqual(flow["datos_reclamo"]["descripcion"], self.TEXT)
        self.assertEqual(flow["category_authority"]["source"], "tenant_ticket_catalog")

    def test_unpublished_menu_source_never_enables_empty_catalog_fallback(self):
        self.fixture.post("Hola")
        original_menu = municipal._get_reclamos_menu
        def different_menu(context=None):
            response = original_menu(context)
            response["fuente"] = "unknown_unpublished_menu"
            return response
        with patch.object(municipal, "_get_reclamos_menu", side_effect=different_menu):
            context, _ = self.fixture.post(self.TEXT)
        self.assertNotIn("categoria", context["reclamo_flow_v2"]["datos_reclamo"])

    def test_cancel_removes_same_draft_and_category_authority(self):
        self.assert_address_after_hello()
        context, _ = self.fixture.post("cancelar")
        self.assertFalse(context.get("reclamo_flow_v2", {}).get("state"))
