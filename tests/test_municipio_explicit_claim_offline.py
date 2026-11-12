"""Real webhook/municipal flow with only provider and media boundaries synthetic.

Run in the disposable profile_acceptance_runtime before importing this module.
Signature acceptance is a test boundary, not provider delivery verification.
"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from sqlalchemy import text
from sqlalchemy.orm import Query

from tests import test_whatsapp_webhook as webhook_tests
from models import CategoriaTicket, ChatSessionContext, MunicipioTicket, TenantProfile, TenantTicket, db
from services.constants import CONTEXTO_MUNICIPIO
from services import municipio_responder as municipal
from services import llm_orchestrator as orchestrator


class _ImmediateTimer:
    def __init__(self, delay, callback):
        self.callback = callback

    def start(self):
        self.callback()


class _UnavailableProvider(RuntimeError):
    code = "synthetic_provider_unavailable"
    safe_to_fallback = False


class ExplicitClaimOfflineHttpTest(unittest.TestCase):
    def setUp(self):
        webhook_tests.TestConfig.SESSION_TYPE = "null"
        webhook_tests.TestConfig.RATELIMIT_STORAGE_URI = "memory://"
        self.fixture = webhook_tests.WhatsAppWebhookTestCase("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.assertEqual(db.engine.url.database, ":memory:")
        self.fixture._set_owner_tipo_chat("municipio")
        self.tenant = self.fixture._attach_tenant_to_owner(slug="explicit-claim-fixture", tipo="municipio")
        self.fixture._create_confirmed_session()
        session = self.session()
        session.tenant_id = self.tenant.id
        data = dict(session.context_data)
        data["profile_name"] = "Persona Prueba"
        session.context_data = data
        db.session.commit()
        self.fixture.app.config.update(
            WELCOME_TEMPLATE_SID=None, WELCOME_MEDIA_URL=None, WELCOME_AUDIO_URL=None,
            WHATSAPP_MENU_AUDIO_ENABLED=False, WHATSAPP_AUDIO_ENABLED=False,
            WELCOME_MESSAGE_DELAY_SECONDS=0,
        )
        municipal.MUNICIPIO_RESPONSE_CACHE.clear()
        self.calls = []
        self.sequence = 0
        self.provider_calls = 0

        def send(**kwargs):
            self.calls.append(dict(kwargs))
            return SimpleNamespace(sid="SM" + str(len(self.calls)).zfill(32), status="sent")

        def provider(*args, **kwargs):
            self.provider_calls += 1
            raise _UnavailableProvider("synthetic unavailable provider; no transport")

        self.fixture.mock_twilio_create.side_effect = send
        boundaries = [
            patch("routes.whatsapp_webhook.threading.Timer", _ImmediateTimer),
            patch("routes.whatsapp_webhook._prepare_cached_welcome_audio", return_value=None),
            patch("routes.whatsapp_webhook._ensure_welcome_audio_payload", return_value=None),
            patch.object(orchestrator, "llamar_openai", side_effect=provider),
            patch.object(orchestrator, "llm_provider_network_allowed", return_value=True),
            patch.object(orchestrator, "_task_provider_order", return_value=["openai"]),
            patch.object(municipal, "extract_complaint_details_llm", return_value={}),
            patch.object(municipal, "extract_multiple_contact_details_llm", return_value={}),
        ]
        for boundary in boundaries:
            mocked = boundary.start()
            if boundary.attribute == "extract_complaint_details_llm":
                self.complaint_extractor = mocked
            self.addCleanup(boundary.stop)

    def session(self):
        return ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).one()

    def category(self, name, tenant=None, *, kind="ticket"):
        row = CategoriaTicket(nombre=name, tenant_id=(tenant or self.tenant).id, tipo=kind)
        db.session.add(row)
        db.session.commit()

    def post(self, text, *, same_sid=False):
        if not same_sid:
            self.sequence += 1
        before = len(self.calls)
        response = self.fixture.client.post(
            "/webhook/whatsapp",
            data={
                "To": "whatsapp:" + self.fixture.test_whatsapp_number_str,
                "From": "whatsapp:" + self.fixture.test_user_number_str,
                "Body": text, "ProfileName": "Persona Prueba",
                "MessageSid": "SM" + str(self.sequence).zfill(32),
            },
            headers={"X-Twilio-Signature": "synthetic-fixture-signature"},
        )
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        session = self.session()
        self.assertEqual(session.tenant_id, self.tenant.id)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(TenantTicket.query.count(), 0)
        return (
            (session.context_data or {}).get(CONTEXTO_MUNICIPIO, {}),
            "\n".join(str(call.get("body") or "") for call in self.calls[before:]),
        )

    def assert_address_draft(self, ctx, original, category="Arbolado"):
        flow = ctx["reclamo_flow_v2"]
        self.assertEqual(flow["state"], "ESPERANDO_DIRECCION")
        self.assertEqual(flow["datos_reclamo"]["categoria"], category)
        self.assertEqual(flow["datos_reclamo"]["descripcion"], original)
        self.assertNotIn("direccion", flow["datos_reclamo"])
        self.assertEqual(self.provider_calls, 0)

    def test_exact_owner_sentence_reaches_address_without_AI(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, body = self.post(original)
        self.assert_address_draft(ctx, original)
        self.assertIn("direcci", body.lower())
        self.assertNotIn("asistentes de IA", body)

    def set_general_state(self):
        row = self.session()
        data = deepcopy(row.context_data)
        data.setdefault(CONTEXTO_MUNICIPIO, {})["estado_conversacion"] = "CONVERSACION_GENERAL_LLM"
        row.context_data = data
        db.session.commit()

    def test_retry_after_general_AI_state_retains_original_draft_and_continues_same_identity(self):
        self.set_general_state()
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, body = self.post(original)
        self.assert_address_draft(ctx, original)
        flow = deepcopy(ctx["reclamo_flow_v2"])
        self.assertNotIn("asistentes de IA", body)
        with patch("services.address_normalizer.normalize_and_geocode", return_value=None):
            ctx, _ = self.post("Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_FOTO")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["direccion"], "Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["confirmation_id"], flow["confirmation_id"])
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["descripcion"], original)
        self.assertEqual(self.provider_calls, 0)
        self.complaint_extractor.assert_not_called()

    def test_empty_legacy_state_keeps_fresh_explicit_command_behavior(self):
        row = self.session()
        data = deepcopy(row.context_data)
        data[CONTEXTO_MUNICIPIO] = {"estado_conversacion": ""}
        row.context_data = data
        db.session.commit()
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, _ = self.post(original)
        self.assert_address_draft(ctx, original)
        flow = deepcopy(ctx["reclamo_flow_v2"])
        with patch("services.address_normalizer.normalize_and_geocode", return_value=None):
            ctx, _ = self.post("Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_FOTO")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["direccion"], "Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["confirmation_id"], flow["confirmation_id"])
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["descripcion"], original)
        self.assertEqual(self.provider_calls, 0)
        self.complaint_extractor.assert_not_called()

    def test_general_AI_state_information_does_not_gain_model_claim_bootstrap(self):
        self.set_general_state()
        ctx, _ = self.post("cual es el horario del vivero municipal")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))
        self.complaint_extractor.assert_not_called()

    def test_general_AI_state_status_does_not_initialize_claim(self):
        self.set_general_state()
        ctx, _ = self.post("quiero consultar el estado de mi reclamo por un arbol caido")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))
        self.complaint_extractor.assert_not_called()

    def test_general_AI_state_with_active_draft_does_not_restart_it(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, _ = self.post(original)
        before = deepcopy(ctx["reclamo_flow_v2"])
        self.set_general_state()
        ctx, _ = self.post("quiero hacer un reclamo por una luminaria rota")
        self.assertEqual(ctx["reclamo_flow_v2"], before)
        self.assertEqual(self.provider_calls, 0)
        self.complaint_extractor.assert_not_called()

    def test_accents_and_punctuation_preserve_original_description(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "¡Quisiera iniciar un reclamo sobre un árbol caído!"
        ctx, _ = self.post(original)
        self.assert_address_draft(ctx, original)

    def test_other_configured_category_uses_exact_catalog_name(self):
        self.category("Luminaria")
        self.post("Hola")
        original = "Necesito registrar un reclamo de luminaria quemada."
        ctx, _ = self.post(original)
        self.assert_address_draft(ctx, original, "Luminaria")

    def test_affirmative_command_can_describe_a_tree_that_does_not_allow_passage(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol que no deja pasar"
        ctx, _ = self.post(original)
        self.assert_address_draft(ctx, original)

    def test_information_catalog_category_cannot_preselect_claim_category(self):
        self.category("Arbolado", kind="informacion")
        self.post("Hola")
        ctx, _ = self.post("quiero hacer un reclamo por un arbol caido")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_CATEGORIA")
        self.assertNotIn("categoria", ctx["reclamo_flow_v2"]["datos_reclamo"])
        self.assertEqual(self.provider_calls, 0)

    def test_failed_optional_SQL_read_preserves_dedup_durable_draft_and_continuation(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        real_all = Query.all
        failures = []

        def fail_category_read(query):
            if any(item.get("entity") is CategoriaTicket for item in query.column_descriptions):
                failures.append(True)
                # Real SQLAlchemy/SQLite OperationalError in the nested read,
                # not a Python stand-in for a failed database transaction.
                db.session.execute(text("SELECT * FROM missing_explicit_claim_category_fixture"))
            return real_all(query)

        with patch.object(Query, "all", fail_category_read), patch.object(
            db.session, "rollback", wraps=db.session.rollback
        ) as global_rollback:
            ctx, _ = self.post(original)
            global_rollback.assert_not_called()
        self.assertEqual(len(failures), 1)
        self.assertTrue(db.session.is_active)
        flow = deepcopy(ctx["reclamo_flow_v2"])
        self.assertEqual(flow["state"], "ESPERANDO_CATEGORIA")
        self.assertEqual(flow["datos_reclamo"]["descripcion"], original)
        self.assertNotIn("categoria", flow["datos_reclamo"])
        self.assertIn("SM" + str(self.sequence).zfill(32), self.session().context_data["processed_message_sids"])
        before = len(self.calls)
        ctx, _ = self.post(original, same_sid=True)
        self.assertEqual(len(self.calls), before)
        self.assertEqual(ctx["reclamo_flow_v2"], flow)
        ctx, _ = self.post("2")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_DIRECCION")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["descripcion"], original)
        self.assertEqual(ctx["reclamo_flow_v2"]["confirmation_id"], flow["confirmation_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_empty_tenant_catalog_uses_published_menu_authority_without_AI(self):
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, body = self.post(original)
        self.assert_address_draft(ctx, original)
        authority = ctx["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["source"], "published_institutional_menu")
        self.assertEqual(authority["tenant_id"], self.tenant.id)
        self.assertIsNone(authority["category_id"])
        self.assertNotIn("opción para tu reclamo", body)
        self.assertEqual(self.provider_calls, 0)

    def test_another_organization_category_does_not_supply_current_tenant_authority(self):
        other = TenantProfile(
            slug="other-explicit-fixture", nombre="Otra organización", tipo="municipio",
            municipio_id=self.fixture.mock_client_user.id,
        )
        db.session.add(other)
        db.session.commit()
        self.category("Arbolado", other)
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, _ = self.post(original)
        self.assert_address_draft(ctx, original)
        authority = ctx["reclamo_flow_v2"]["category_authority"]
        self.assertEqual(authority["tenant_id"], self.tenant.id)
        self.assertEqual(authority["source"], "published_institutional_menu")
        self.assertIsNone(authority["category_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_ambiguous_issues_ask_category_without_AI(self):
        self.category("Arbolado")
        self.category("Luminaria")
        self.post("Hola")
        ctx, _ = self.post("quiero hacer un reclamo por un arbol caido y una luminaria rota")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_CATEGORIA")
        self.assertNotIn("categoria", ctx["reclamo_flow_v2"]["datos_reclamo"])
        self.assertEqual(self.provider_calls, 0)

    def test_information_question_does_not_initialize_claim(self):
        self.category("Arbolado")
        self.post("Hola")
        ctx, _ = self.post("cual es el horario del vivero municipal")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))

    def test_negated_command_does_not_initialize_claim(self):
        self.category("Arbolado")
        self.post("Hola")
        ctx, _ = self.post("no quiero hacer un reclamo por un arbol caido")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))

    def test_negated_registration_after_issue_does_not_initialize_claim(self):
        self.category("Arbolado")
        self.post("Hola")
        ctx, _ = self.post("quiero hacer un reclamo por un arbol caido pero no quiero registrarlo")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))

    def test_status_question_does_not_initialize_claim(self):
        self.category("Arbolado")
        self.post("Hola")
        ctx, _ = self.post("quiero consultar el estado de mi reclamo por un arbol caido")
        self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))

    def test_menu_and_cancel_controls_do_not_initialize_claim(self):
        self.post("Hola")
        for text in ("menú", "cancelar"):
            with self.subTest(control=text):
                ctx, _ = self.post(text)
                self.assertFalse(ctx.get("reclamo_flow_v2", {}).get("state"))
        self.assertEqual(self.provider_calls, 0)

    def test_retry_same_message_keeps_existing_confirmation_identity(self):
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, _ = self.post(original)
        before = deepcopy(ctx["reclamo_flow_v2"])
        ctx, _ = self.post(original, same_sid=True)
        self.assertEqual(ctx["reclamo_flow_v2"], before)
        self.assertEqual(self.provider_calls, 0)

    def test_mismatched_authoritative_context_does_not_initialize_draft(self):
        self.category("Arbolado")
        self.post("Hola")
        data = deepcopy(self.session().context_data)
        before = deepcopy(data)
        context = {
            "tenant_profile": self.tenant, "tenant_id": self.tenant.id + 100,
            "user_obj": self.fixture.mock_client_user,
            "chat_db_context_data": data,
        }
        result = municipal._try_start_explicit_reclamo_from_text(
            "quiero hacer un reclamo por un arbol caido", context, self.session()
        )
        self.assertIsNone(result)
        self.assertEqual(data, before)
        self.assertEqual(self.provider_calls, 0)

    def test_active_draft_is_not_restarted_by_explicit_helper(self):
        self.category("Arbolado")
        self.post("Hola")
        self.post("quiero hacer un reclamo por un arbol caido")
        data = deepcopy(self.session().context_data)
        before = deepcopy(data)
        context = {
            "tenant_profile": self.tenant, "tenant_id": self.tenant.id,
            "user_obj": self.fixture.mock_client_user,
            "chat_db_context_data": data,
        }
        result = municipal._try_start_explicit_reclamo_from_text(
            "quiero hacer un reclamo por una luminaria rota", context, self.session()
        )
        self.assertIsNone(result)
        self.assertEqual(data, before)
        self.assertEqual(self.provider_calls, 0)

    def test_new_Hola_preserves_pending_category_and_original_draft(self):
        # Welcome recovery is explicitly scoped to canonical JUNÍ; the
        # generic tenant controls above do not broaden that behavior.
        self.tenant.slug = "junin"
        db.session.commit()
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido y una luminaria rota"
        ctx, _ = self.post(original)
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_CATEGORIA")
        self.assertNotIn("categoria", ctx["reclamo_flow_v2"]["datos_reclamo"])
        before = deepcopy(ctx["reclamo_flow_v2"])
        ctx, body = self.post("Hola")
        self.assertEqual(ctx["reclamo_flow_v2"], before)
        self.assertTrue(body)
        self.assertEqual(self.provider_calls, 0)
        ctx, _ = self.post("2")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_DIRECCION")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["descripcion"], original)
        self.assertEqual(ctx["reclamo_flow_v2"]["confirmation_id"], before["confirmation_id"])
        self.assertEqual(self.provider_calls, 0)

    def test_new_Hola_preserves_pending_address_and_is_not_an_address(self):
        self.tenant.slug = "junin"
        db.session.commit()
        self.category("Arbolado")
        self.post("Hola")
        original = "quiero hacer un reclamo por un arbol caido"
        ctx, _ = self.post(original)
        before = deepcopy(ctx["reclamo_flow_v2"])
        ctx, body = self.post("Hola")
        self.assertEqual(ctx["reclamo_flow_v2"], before)
        self.assert_address_draft(ctx, original)
        self.assertIn("direcci", body.lower())
        with patch("services.address_normalizer.normalize_and_geocode", return_value=None):
            ctx, _ = self.post("Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["state"], "ESPERANDO_FOTO")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["direccion"], "Sarmiento 123")
        self.assertEqual(ctx["reclamo_flow_v2"]["datos_reclamo"]["descripcion"], original)
        self.assertEqual(ctx["reclamo_flow_v2"]["confirmation_id"], before["confirmation_id"])
        self.assertEqual(self.provider_calls, 0)


if __name__ == "__main__":
    unittest.main()
