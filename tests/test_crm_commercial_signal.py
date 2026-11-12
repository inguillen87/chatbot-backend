"""Original CRM caller and persistence with disposable SQLite; no provider I/O.

Standalone runs must call profile_acceptance_runtime.prepare_process first.
These checks certify CRM classification, not WhatsApp delivery or marketing consent.
"""
import unittest
from unittest.mock import patch

from tests import test_whatsapp_webhook as webhook_tests
from extensions import db
from models import ChatSessionContext, Notification
from models_memory import Contact, ContactSnapshot, InteractionEvent
from routes.whatsapp_webhook import _record_chatboc_demo_engagement, _upsert_chatboc_demo_crm_contact
from services.crm_intelligence import classify_reason, lead_temperature


class CommercialSignalUnitTests(unittest.TestCase):
    def test_technical_event_does_not_turn_greeting_into_sales(self):
        self.assertEqual(classify_reason("Hola", {"event_type": "chatboc_demo_inbound"})[0], "saludo")

    def test_technical_source_does_not_turn_operational_query_into_sales(self):
        self.assertEqual(classify_reason("reclamo por un arbol caido", {"source": "demo_sales_transport"})[0], "reclamo")

    def test_demo_navigation_does_not_turn_operational_query_into_sales(self):
        for action in ("chatboc_demo:gobierno", "chatboc_demo_claim_start", "chatboc_demo_order_start"):
            with self.subTest(action=action):
                self.assertNotEqual(classify_reason("reclamo por un arbol caido", {"action_id": action})[0], "lead_hot")

    def test_known_explicit_sales_actions_remain_hot(self):
        for action in ("chatboc_sales_lead", "capturar_lead_comercial", "chatboc_demo_limit_contact_yes"):
            with self.subTest(action=action):
                intent, _ = classify_reason("me interesa", {"action_id": action})
                self.assertEqual(intent, "lead_hot")
                self.assertEqual(lead_temperature(intent, "me interesa", {"action_id": action}), ("hot", 85))

    def test_explicit_user_sales_text_remains_hot(self):
        for text in ("quiero cotizar el sistema", "quiero una demo", "hablar con ventas", "necesito precio"):
            with self.subTest(text=text):
                intent, _ = classify_reason(text, {"event_type": "whatsapp_inbound"})
                self.assertEqual(intent, "lead_hot")
                self.assertEqual(lead_temperature(intent, text), ("hot", 85))

    def test_unrelated_generic_greeting_and_operational_classification_remain(self):
        self.assertEqual(classify_reason("Hola")[0], "saludo")
        self.assertEqual(lead_temperature("saludo", "Hola"), ("cold", 25))
        self.assertEqual(classify_reason("reclamo por un arbol caido")[0], "reclamo")
        self.assertEqual(lead_temperature("reclamo", "reclamo por un arbol caido"), ("warm", 55))

    def test_engagement_is_warm_and_never_implies_commercial_consent(self):
        self.assertEqual(lead_temperature("saludo", "Hola", {"prospect_engaged": True}), ("warm", 55))
        for value in (False, "true", "false", 1, None):
            with self.subTest(value=value):
                metadata = {"commercial_contact_requested": value, "prospect_engaged": value}
                self.assertEqual(classify_reason("Hola", metadata)[0], "saludo")
                self.assertEqual(lead_temperature("saludo", "Hola", metadata), ("cold", 25))


class DemoCrmCommercialSignalTests(unittest.TestCase):
    def setUp(self):
        webhook_tests.TestConfig.SESSION_TYPE = "null"
        webhook_tests.TestConfig.RATELIMIT_STORAGE_URI = "memory://"
        self.fixture = webhook_tests.WhatsAppWebhookTestCase("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.assertEqual(db.engine.url.database, ":memory:")
        self.tenant = self.fixture._attach_tenant_to_owner(slug="commercial-signal-fixture", tipo="pyme")
        emit = patch("services.crm_intelligence.emit_crm_contact_update")
        self.emit = emit.start()
        self.addCleanup(emit.stop)

    def record(self, text, action_id=None):
        contact = _upsert_chatboc_demo_crm_contact(
            tenant=self.tenant, contact_user=None, phone="+15550001000", profile_name="Persona Prueba",
            message_body=text, action_id=action_id, ticket=None,
            input_context={"content_type": "text", "summary": text},
        )
        db.session.flush()
        snapshot = ContactSnapshot.query.filter_by(contact_id=contact.id).one()
        event = InteractionEvent.query.filter_by(contact_id=contact.id).order_by(InteractionEvent.id.desc()).first()
        self.assertEqual(event.tenant_id, self.tenant.id)
        self.assertEqual(event.metadata_payload["lead_temperature"], contact.preferences["last_lead_temperature"])
        self.assertEqual(snapshot.last_intent, contact.preferences["last_intent"])
        return contact, snapshot, event

    def test_hola_stays_warm_without_commercial_contact_request(self):
        contact, snapshot, event = self.record("Hola")
        self.assertFalse(contact.preferences["commercial_contact_requested"])
        self.assertEqual(contact.preferences["marketing_consent_status"], "unknown")
        self.assertFalse(event.metadata_payload["commercial_contact_requested"])
        self.assertTrue(event.metadata_payload["prospect_engaged"])
        self.assertEqual((contact.preferences["last_intent"], contact.preferences["lead_temperature"], contact.preferences["lead_score"]), ("saludo", "warm", 55))
        self.assertNotIn("contactar_ventas", snapshot.suggested_actions)

    def test_demo_navigation_stays_warm_without_commercial_request(self):
        contact, snapshot, event = self.record("Gobiernos y municipios", "chatboc_demo:gobierno")
        self.assertFalse(contact.preferences["commercial_contact_requested"])
        self.assertEqual(contact.preferences["lead_temperature"], "warm")
        self.assertNotIn("contactar_ventas", snapshot.suggested_actions)

    def test_operational_question_preserves_operational_intent(self):
        contact, snapshot, event = self.record("quiero hacer un reclamo por un arbol caido")
        self.assertFalse(contact.preferences["commercial_contact_requested"])
        self.assertEqual((snapshot.last_intent, contact.preferences["lead_temperature"]), ("reclamo", "warm"))
        self.assertIn("revisar_ticket", snapshot.suggested_actions)
        self.assertNotIn("contactar_ventas", snapshot.suggested_actions)

    def test_sales_cta_marks_commercial_request_hot(self):
        contact, snapshot, event = self.record("me interesa", "chatboc_sales_lead")
        self.assertTrue(contact.preferences["commercial_contact_requested"])
        self.assertTrue(event.metadata_payload["commercial_contact_requested"])
        self.assertEqual(contact.preferences["marketing_consent_status"], "unknown")
        self.assertEqual((snapshot.last_intent, contact.preferences["lead_temperature"], contact.preferences["lead_score"]), ("lead_hot", "hot", 85))
        self.assertIn("contactar_ventas", snapshot.suggested_actions)

    def test_explicit_sales_text_is_hot_without_inventing_contact_consent(self):
        contact, snapshot, event = self.record("quiero cotizar el sistema")
        self.assertFalse(contact.preferences["commercial_contact_requested"])
        self.assertEqual(contact.preferences["marketing_consent_status"], "unknown")
        self.assertEqual((snapshot.last_intent, contact.preferences["lead_temperature"]), ("lead_hot", "hot"))

    def test_hola_after_sales_cta_keeps_contact_priority_but_not_false_sales_intent(self):
        self.record("me interesa", "chatboc_sales_lead")
        contact, snapshot, event = self.record("Hola")
        self.assertTrue(contact.preferences["commercial_contact_requested"])
        self.assertFalse(contact.preferences["last_commercial_contact_requested"])
        self.assertEqual((snapshot.last_intent, contact.preferences["lead_temperature"]), ("saludo", "hot"))
        self.assertEqual((event.metadata_payload["lead_temperature"], event.metadata_payload["lead_score"]), ("warm", 55))
        self.assertFalse(event.metadata_payload["commercial_contact_requested"])
        self.assertIn("lead_caliente", contact.tags)
        events = InteractionEvent.query.filter_by(contact_id=contact.id).order_by(InteractionEvent.id).all()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].metadata_payload["lead_temperature"], "hot")
        self.assertTrue(events[0].metadata_payload["commercial_contact_requested"])

    def test_operational_message_does_not_erase_previous_sales_request(self):
        self.record("me interesa", "chatboc_sales_lead")
        contact, snapshot, event = self.record("reclamo por un arbol caido")
        self.assertTrue(contact.preferences["commercial_contact_requested"])
        self.assertEqual((snapshot.last_intent, contact.preferences["lead_temperature"]), ("reclamo", "hot"))
        self.assertEqual(event.metadata_payload["lead_temperature"], "warm")
        self.assertFalse(event.metadata_payload["commercial_contact_requested"])

    def test_operator_closed_stage_is_not_reopened_by_a_greeting(self):
        contact, _, _ = self.record("me interesa", "chatboc_sales_lead")
        prefs = dict(contact.preferences)
        prefs.update(conversation_status="perdido", lead_temperature="cold", lead_score=10, stage_updated_at="2026-10-22T10:00:00+00:00")
        contact.preferences = prefs
        db.session.flush()
        contact, snapshot, event = self.record("Hola")
        self.assertEqual(contact.preferences["conversation_status"], "perdido")
        self.assertEqual((contact.preferences["lead_temperature"], contact.preferences["lead_score"]), ("cold", 10))
        self.assertTrue(contact.preferences["commercial_contact_requested"])
        self.assertEqual(event.metadata_payload["lead_temperature"], "warm")

    def test_existing_explicit_marketing_status_survives_greeting_and_sales_cta(self):
        for status in ("opted_out", "opted_in"):
            for text, action in (("Hola", None), ("me interesa", "chatboc_sales_lead")):
                with self.subTest(status=status, action=action):
                    contact, _, _ = self.record("Hola")
                    prefs = dict(contact.preferences)
                    prefs["marketing_consent_status"] = status
                    prefs["marketing_opt_in"] = status == "opted_in"
                    contact.preferences = prefs
                    db.session.flush()
                    contact, _, _ = self.record(text, action)
                    self.assertEqual(contact.preferences["marketing_consent_status"], status)
                    self.assertIs(contact.preferences["marketing_opt_in"], status == "opted_in")


class DemoEngagementConsentTests(unittest.TestCase):
    setUp = DemoCrmCommercialSignalTests.setUp

    def engage(self, text, action_id=None):
        session = ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).first()
        if session is None:
            self.fixture._create_confirmed_session()
            session = ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).one()
            session.tenant_id = self.tenant.id
            db.session.commit()
        user, ticket = _record_chatboc_demo_engagement(
            owner_user=self.fixture.mock_client_user, tenant=self.tenant, session_context=session,
            from_number=self.fixture.test_user_number_str, profile_name="Persona Prueba",
            message_body=text, action_id=action_id, message_sid=None,
            input_context={"content_type": "text", "summary": text, "effective_message": text},
        )
        contact = Contact.query.filter_by(tenant_id=self.tenant.id, phone=self.fixture.test_user_number_str).one()
        self.assertIsNotNone(user)
        self.assertIsNotNone(ticket)
        return user, ticket, contact

    def test_new_sales_cta_creates_commercial_followup_without_marketing_optin(self):
        user, ticket, contact = self.engage("me interesa", "chatboc_sales_lead")
        self.assertFalse(user.acepta_marketing)
        self.assertNotIn("marketing_opt_in", contact.preferences)
        self.assertEqual(contact.preferences["marketing_consent_status"], "unknown")
        self.assertTrue(contact.preferences["commercial_contact_requested"])
        self.assertEqual(contact.preferences["lead_temperature"], "hot")
        self.assertEqual(ticket.estado_cliente, "quiere_contacto")
        notification = Notification.query.filter_by(tenant_id=self.tenant.id).order_by(Notification.id.desc()).first()
        self.assertEqual(notification.channel, "in_app")
        self.assertTrue(notification.metadata_json["commercial_contact_requested"])

    def test_existing_explicit_consent_flags_survive_parent_sales_cta_and_hola(self):
        for user_optin in (False, True):
            for contact_optin in (False, True):
                with self.subTest(user_optin=user_optin, contact_optin=contact_optin):
                    user, _, contact = self.engage("Hola")
                    user.acepta_marketing = user_optin
                    prefs = dict(contact.preferences)
                    prefs["marketing_opt_in"] = contact_optin
                    prefs["marketing_consent_status"] = "opted_in" if contact_optin else "opted_out"
                    contact.preferences = prefs
                    db.session.commit()
                    for text, action in (("me interesa", "chatboc_sales_lead"), ("Hola", None)):
                        user, _, contact = self.engage(text, action)
                        self.assertIs(user.acepta_marketing, user_optin)
                        self.assertIs(contact.preferences["marketing_opt_in"], contact_optin)
                        self.assertEqual(contact.preferences["marketing_consent_status"], "opted_in" if contact_optin else "opted_out")
