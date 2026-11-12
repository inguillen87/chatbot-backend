"""School intake with real widget/WhatsApp consumers and disposable ORM."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import db
from models import ArchivoAdjunto, ChatSessionContext, PymeTicket, TenantProfile, User
from models_education import SchoolCaseAlias
from routes.whatsapp_webhook import _handle_education_whatsapp_turn
from services.pymes import responder_pyme
from services.ticket_service import servicio_tickets
from tests.test_education_widget_flow import EducationWidgetFlowTest


class EducationContextFirstDraftTest(unittest.TestCase):
    setUp = EducationWidgetFlowTest.setUp
    tearDown = EducationWidgetFlowTest.tearDown

    def session(self, channel):
        session = ChatSessionContext(chat_session_id="context-first-" + channel,
            tenant_id=self.tenant.id, anon_id="anon-edu", context_data={})
        db.session.add(session)
        db.session.commit()
        return session

    def turn(self, channel, session, *, action=None, text="", media=None, location=None,
             actor=None, anon_id="anon-edu"):
        if channel == "widget":
            payload = {"pregunta": text, "action_id": action,
                "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
                "uploaded_file_info": media, "ubicacion_usuario": location}
            return responder_pyme(payload, self.owner, None, viewer_user=actor,
                chat_db_context=session, anon_id=anon_id, channel=channel)
        return _handle_education_whatsapp_turn(session_context=session,
            tenant_profile=self.tenant, owner_user=self.owner, end_user=actor,
            anon_id=anon_id, selected_action_id=action, selected_option=None,
            message_body=text, uploaded_file_info=media, location_info=location)

    @staticmethod
    def option(response, verb):
        return next(item["action_id"] for item in response["options_list"]
                    if item["action_id"].startswith("education_case_" + verb + ":"))

    def media(self, session, *, name="certificado.png", mime="image/png"):
        attachment = ArchivoAdjunto(filename=name, nombre_original=name, mime=mime,
            session_id=session.chat_session_id, url="https://fixture.invalid/" + name)
        db.session.add(attachment)
        db.session.commit()
        return attachment, {"id": attachment.id, "name": name, "mime_type": mime,
                            "url": attachment.url}

    def test_action_with_detail_reviews_without_repeating_or_creating(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                detail = "Alumna de 3A ausente hoy por fiebre."
                response = self.turn(channel, session, action="justificar_inasistencia", text=detail)
                self.assertEqual(response["fuente"], "education_" + channel + "_case_review")
                self.assertIn(detail, response["message_body"])
                self.assertNotIn("_scope", response["data"]["education_context"]["pending_case"])
                self.assertEqual(PymeTicket.query.count(), 0)
                self.assertEqual(SchoolCaseAlias.query.count(), 0)

    def test_text_caption_and_available_transcript_accumulate(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                _, media = self.media(session, name="nota.ogg", mime="audio/ogg")
                media.update(caption="Certificado pendiente para mañana.",
                             transcribed_text="Faltó hoy por indicación médica.")
                response = self.turn(channel, session, action="justificar_inasistencia",
                    text="Alumno de 3A.", media=media)
                for part in ("Alumno de 3A.", media["caption"], media["transcribed_text"]):
                    self.assertIn(part, response["message_body"])
                self.assertEqual(PymeTicket.query.count(), 0)

    def test_uninterpreted_audio_and_zero_coordinates_are_kept_without_invented_detail(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                attachment, media = self.media(session, name="nota.ogg", mime="audio/ogg")
                before = PymeTicket.query.count()
                response = self.turn(channel, session, action="justificar_inasistencia",
                    media=media, location={"lat": 0, "lon": 0})
                self.assertEqual(response["fuente"], "education_" + channel + "_case_prompt")
                self.assertIn("breve descripción", response["message_body"])
                self.assertEqual(PymeTicket.query.count(), before)
                response = self.turn(channel, session, text="Alumno de 3A ausente hoy por fiebre.")
                self.assertIn("nota.ogg", response["message_body"])
                self.assertEqual(session.context_data["education_pending_case"]["location"],
                                 {"latitude": 0.0, "longitude": 0.0})
                before = PymeTicket.query.count()
                with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
                    self.turn(channel, session, action=self.option(response, "confirm"), text="Confirmar caso")
                self.assertEqual(PymeTicket.query.count(), before + 1)
                ticket = PymeTicket.query.order_by(PymeTicket.id.desc()).first()
                self.assertEqual((ticket.latitud, ticket.longitud), (0.0, 0.0))
                self.assertEqual(db.session.get(ArchivoAdjunto, attachment.id).pyme_ticket_id, ticket.id)

    def test_detail_step_requires_current_revision_confirmation_and_replay_does_not_create(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                self.turn(channel, session, action="justificar_inasistencia")
                response = self.turn(channel, session, text="Alumno de 3A ausente hoy por fiebre.")
                before = PymeTicket.query.count()
                confirm = self.option(response, "confirm")
                with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
                    response = self.turn(channel, session, action=confirm, text="1")
                    self.assertEqual(PymeTicket.query.count(), before + 1)
                    ticket = PymeTicket.query.order_by(PymeTicket.id.desc()).first()
                    self.assertEqual(ticket.categoria, "inasistencia")
                    self.assertNotIn("\n1", ticket.pregunta)
                    self.assertEqual(SchoolCaseAlias.query.filter_by(ticket_id=ticket.id).count(), 1)
                    self.turn(channel, session, action=confirm, text="1")
                self.assertEqual(PymeTicket.query.count(), before + 1)

    def test_edit_preserves_attachment_and_location_but_invalidates_stale_confirmation(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                _, media = self.media(session)
                response = self.turn(channel, session, action="justificar_inasistencia",
                    text="Alumno de 3A ausente hoy.", media=media,
                    location={"latitude": -32.9, "longitude": -68.8})
                stale = self.option(response, "confirm")
                self.turn(channel, session, action=self.option(response, "edit"), text="Editar detalle")
                response = self.turn(channel, session, text="Alumno de 4B ausente ayer.")
                self.assertIn("4B", response["message_body"])
                self.assertNotIn("3A", response["message_body"])
                self.assertIn("certificado.png", response["message_body"])
                self.assertIn("-32.9", response["message_body"])
                new_confirm = self.option(response, "confirm")
                response = self.turn(channel, session, action=stale, text="Confirmar caso")
                self.assertEqual(PymeTicket.query.count(), 0)
                self.assertEqual(self.option(response, "confirm"), new_confirm)

    def test_new_evidence_with_confirmation_requires_review_again(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                old_confirm = self.option(response, "confirm")
                _, media = self.media(session)
                response = self.turn(channel, session, action=old_confirm, text="Confirmar caso", media=media)
                self.assertEqual(PymeTicket.query.count(), 0)
                self.assertNotEqual(self.option(response, "confirm"), old_confirm)
                self.assertIn("certificado.png", response["message_body"])

    def test_cancel_discards_draft_and_old_confirmation_is_not_reused(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                confirm = self.option(response, "confirm")
                self.turn(channel, session, action=self.option(response, "cancel"), text="Cancelar")
                self.assertNotIn("education_pending_case", session.context_data)
                self.turn(channel, session, action=confirm, text="Confirmar caso")
                self.assertEqual(PymeTicket.query.count(), 0)

    def test_scope_changes_cannot_confirm_original_draft(self):
        actor = User(name="Otra persona", email="scope-actor@example.invalid", password_hash="hash", tipo_chat="pyme")
        db.session.add(actor)
        db.session.commit()
        for channel in ("widget", "whatsapp"):
            for field in ("tenant_id", "owner_id", "actor_id", "anon_id", "session_id"):
                with self.subTest(channel=channel, changed=field):
                    session = self.session(channel + "-" + field)
                    response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                    confirm = self.option(response, "confirm")
                    # The stored authority was changed/retired, while the live
                    # tenant/owner/session remain canonical for this turn.
                    data = deepcopy(session.context_data)
                    data["education_pending_case"]["_scope"][field] = "retired-scope"
                    session.context_data = data
                    response = self.turn(channel, session, action=confirm, text="Confirmar caso")
                    self.assertEqual(PymeTicket.query.count(), 0)
                    self.assertNotIn("education_pending_case", session.context_data)
                    self.assertTrue(response["fuente"].endswith("_case_expired"))

    def test_changed_live_session_actor_or_tenant_is_rejected(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                session.anon_id = "another-actor"
                response = self.turn(channel, session, action=self.option(response, "confirm"), text="Confirmar caso")
                self.assertTrue(response["fuente"].endswith("_case_scope_invalid"))
                self.assertEqual(PymeTicket.query.count(), 0)

    def test_expired_draft_cannot_create(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                data = deepcopy(session.context_data)
                data["education_pending_case"]["created_at"] -= 1801
                session.context_data = data
                response = self.turn(channel, session, action=self.option(response, "confirm"), text="Confirmar caso")
                self.assertTrue(response["fuente"].endswith("_case_expired"))
                self.assertEqual(PymeTicket.query.count(), 0)

    def test_button_label_and_send_detail_are_not_invented_case_content(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="justificar_inasistencia")
                self.assertTrue(response["fuente"].endswith("_case_prompt"))
                response = self.turn(channel, session, action="education_send_detail", text="Enviar detalle")
                self.assertEqual(session.context_data["education_pending_case"]["details"], [])
                self.assertEqual(PymeTicket.query.count(), 0)

    def test_foreign_or_missing_attachment_is_not_linked_and_ack_reports_it(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                foreign = ArchivoAdjunto(filename="ajeno.png", session_id="otra-sesion", url="https://fixture.invalid/ajeno.png")
                db.session.add(foreign)
                db.session.commit()
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.",
                    media={"id": foreign.id, "name": "ajeno.png", "url": foreign.url, "mime_type": "image/png"})
                response = self.turn(channel, session,
                    media={"id": 999999, "name": "inexistente.png", "url": "https://fixture.invalid/missing.png", "mime_type": "image/png"})
                with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
                    response = self.turn(channel, session, action=self.option(response, "confirm"), text="Confirmar caso")
                self.assertIn("No pude vincular", response["message_body"])
                self.assertIsNone(db.session.get(ArchivoAdjunto, foreign.id).pyme_ticket_id)

    def test_whatsapp_available_transcript_in_body_and_media_is_stored_once(self):
        session = self.session("whatsapp")
        transcript = "Alumno de 3A ausente hoy por indicación médica."
        _, media = self.media(session, name="audio.ogg", mime="audio/ogg")
        media["transcribed_text"] = transcript
        response = self.turn("whatsapp", session, action="justificar_inasistencia", text=transcript, media=media)
        self.assertEqual(session.context_data["education_pending_case"]["details"], [transcript])
        self.assertEqual(response["message_body"].count(transcript), 1)
        self.assertEqual(PymeTicket.query.count(), 0)

    def test_retired_live_session_or_owner_cannot_confirm(self):
        other_owner = User(name="Otro colegio", email="other-school@example.invalid", password_hash="hash", tipo_chat="pyme")
        db.session.add(other_owner)
        db.session.flush()
        other_tenant = TenantProfile(slug="other-school", nombre="Otro colegio", tipo="pyme",
            pyme_id=other_owner.id, vertical="educacion", subvertical="colegio_general",
            capabilities_json={"education": {"enabled": True}}, is_active=True)
        db.session.add(other_tenant)
        db.session.commit()
        for channel in ("widget", "whatsapp"):
            for change in ("session", "tenant", "owner"):
                with self.subTest(channel=channel, change=change):
                    session = self.session(channel + "-live-" + change)
                    response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                    confirm = self.option(response, "confirm")
                    original_owner, original_tenant = self.owner, self.tenant
                    if change == "session":
                        session.chat_session_id += "-retired"
                    elif change == "tenant":
                        session.tenant_id = None
                    else:
                        self.owner, self.tenant = other_owner, other_tenant
                    response = self.turn(channel, session, action=confirm, text="Confirmar caso")
                    self.assertEqual(PymeTicket.query.count(), 0)
                    self.assertTrue(response["fuente"].endswith(("_case_expired", "_case_scope_invalid")))
                    self.owner, self.tenant = original_owner, original_tenant

    def test_nonzero_and_absent_coordinates_survive_persistence(self):
        for channel in ("widget", "whatsapp"):
            for label, location, expected in (("provided", {"lat": -32.9, "lon": -68.8}, (-32.9, -68.8)),
                                              ("absent", None, (None, None))):
                with self.subTest(channel=channel, location=label):
                    session = self.session(channel + "-coordinates-" + label)
                    response = self.turn(channel, session, action="justificar_inasistencia",
                        text="Alumno de 3A ausente hoy.", location=location)
                    with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
                        self.turn(channel, session, action=self.option(response, "confirm"), text="Confirmar caso")
                    ticket = PymeTicket.query.order_by(PymeTicket.id.desc()).first()
                    self.assertEqual((ticket.latitud, ticket.longitud), expected)

    def test_invalid_coordinates_do_not_replace_valid_saved_location(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.",
                          location={"lat": -32.9, "lon": -68.8})
                response = self.turn(channel, session, location={"lat": "NaN", "lon": 181})
                self.assertEqual(session.context_data["education_pending_case"]["location"],
                                 {"latitude": -32.9, "longitude": -68.8})
                self.assertNotIn("NaN", response["message_body"])

    def test_whatsapp_explicit_secretary_handoff_retains_its_detail_continuation(self):
        session = self.session("whatsapp")
        response = self.turn("whatsapp", session, action="derivar_humano", text="Hablar con secretaria")
        self.assertEqual(response["fuente"], "education_whatsapp_intent_prompt")
        self.assertEqual(PymeTicket.query.count(), 0)
        with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
            response = self.turn("whatsapp", session, text="Necesito hablar con secretaría por una autorización.")
        self.assertEqual(PymeTicket.query.count(), 1)
        self.assertEqual(PymeTicket.query.first().categoria, "secretaria")
        self.assertEqual(response["fuente"], "education_whatsapp_case_created")

    def test_whatsapp_secretary_handoff_uses_detail_and_media_from_same_turn(self):
        session = self.session("whatsapp")
        attachment, media = self.media(session, name="autorizacion.png")
        media["caption"] = "La autorización está pendiente de firma."
        reason = "Necesito hablar con secretaría por una autorización."
        with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
            response = self.turn("whatsapp", session, action="derivar_humano", text=reason, media=media)
        self.assertEqual(response["fuente"], "education_whatsapp_case_created")
        self.assertEqual(PymeTicket.query.count(), 1)
        ticket = PymeTicket.query.first()
        self.assertIn(reason, ticket.pregunta)
        self.assertIn(media["caption"], ticket.pregunta)
        self.assertEqual(db.session.get(ArchivoAdjunto, attachment.id).pyme_ticket_id, ticket.id)

    def test_registration_failure_keeps_draft_and_does_not_blind_retry(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                confirm = self.option(response, "confirm")
                with patch.object(servicio_tickets, "crear_nuevo_ticket", return_value=None) as create:
                    response = self.turn(channel, session, action=confirm, text="Confirmar caso")
                    self.assertIn("Conservo el borrador", response["message_body"])
                    self.turn(channel, session, action=confirm, text="Confirmar caso")
                    self.assertEqual(create.call_count, 1)
                self.assertEqual(PymeTicket.query.count(), 0)
                self.assertEqual(session.context_data["education_pending_case"]["status"], "registration_uncertain")

    def test_alias_error_after_ticket_commit_does_not_repeat_creation(self):
        for channel in ("widget", "whatsapp"):
            with self.subTest(channel=channel):
                session = self.session(channel)
                response = self.turn(channel, session, action="justificar_inasistencia", text="Alumno de 3A ausente hoy.")
                confirm = self.option(response, "confirm")
                target = "services.pymes" if channel == "widget" else "routes.whatsapp_webhook"
                before = PymeTicket.query.count()
                with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None), \
                     patch(target + ".create_school_case_alias_for_ticket", side_effect=RuntimeError("synthetic alias failure")):
                    with self.assertRaises(RuntimeError):
                        self.turn(channel, session, action=confirm, text="Confirmar caso")
                self.assertEqual(PymeTicket.query.count(), before + 1)
                response = self.turn(channel, session, action=confirm, text="Confirmar caso")
                self.assertTrue(response["fuente"].endswith("_case_uncertain"))
                self.assertEqual(PymeTicket.query.count(), before + 1)


class EducationContextFirstHttpTest(unittest.TestCase):
    def setUp(self):
        from tests import test_whatsapp_webhook as fixture_module
        from models_education import School, Campus
        fixture_module.TestConfig.SESSION_TYPE = "null"
        fixture_module.TestConfig.RATELIMIT_STORAGE_URI = "memory://"
        self.fixture = fixture_module.WhatsAppWebhookTestCase("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.tenant = self.fixture._attach_tenant_to_owner(slug="school-context-http", tipo="pyme", plan="full")
        self.tenant.vertical = "educacion"
        self.tenant.capabilities_json = {"education": {"enabled": True}}
        school = School(tenant_id=self.tenant.id, name="Colegio HTTP de prueba", status="active")
        db.session.add(school)
        db.session.flush()
        db.session.add(Campus(school_id=school.id, name="Sede de prueba", is_main=True))
        self.fixture._create_confirmed_session()
        session = self.session()
        session.tenant_id = self.tenant.id
        data = dict(session.context_data)
        data["last_options_sent"] = [{"texto": "Justificar inasistencia", "action_id": "justificar_inasistencia"}]
        session.context_data = data
        db.session.commit()
        self.fixture.app.config.update(WELCOME_TEMPLATE_SID=None, WELCOME_MEDIA_URL=None,
            WELCOME_AUDIO_URL=None, WHATSAPP_MENU_AUDIO_ENABLED=False, WHATSAPP_AUDIO_ENABLED=False,
            WELCOME_MESSAGE_DELAY_SECONDS=0)
        self.fixture.mock_validator.validate.return_value = True
        self.sequence = 0
        self.calls = []

        def send(**kwargs):
            self.calls.append(dict(kwargs))
            return SimpleNamespace(sid="SM" + str(len(self.calls)).zfill(32), status="sent")

        self.fixture.mock_twilio_create.side_effect = send
        from tests.test_municipio_explicit_claim_offline import _ImmediateTimer
        for boundary in (
            patch("routes.whatsapp_webhook.threading.Timer", _ImmediateTimer),
            patch("routes.whatsapp_webhook._prepare_cached_welcome_audio", return_value=None),
            patch("routes.whatsapp_webhook._ensure_welcome_audio_payload", return_value=None),
            patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None),
            patch("services.llm_orchestrator.llamar_openai", side_effect=AssertionError("school structured flow must not call AI")),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    def session(self):
        return ChatSessionContext.query.filter_by(anon_id=self.fixture.test_user_number_str).one()

    def post(self, action, body):
        self.sequence += 1
        response = self.fixture.client.post("/webhook/whatsapp", data={
            "To": "whatsapp:" + self.fixture.test_whatsapp_number_str,
            "From": "whatsapp:" + self.fixture.test_user_number_str,
            "MessageSid": "SM_SCHOOL_CONTEXT_" + str(self.sequence),
            "ButtonPayload": action, "Body": body, "ProfileName": "Persona de prueba",
        }, headers={"X-Twilio-Signature": "synthetic-fixture-signature"})
        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        return self.session()

    def test_dynamic_confirmation_id_reaches_real_webhook_and_creates_once(self):
        detail = "Alumno de 3A ausente hoy por fiebre."
        session = self.post("justificar_inasistencia", detail)
        pending = session.context_data["education_pending_case"]
        self.assertEqual(pending["status"], "awaiting_confirmation")
        self.assertIn(detail, pending["details"])
        self.assertEqual(PymeTicket.query.count(), 0)
        self.assertEqual(SchoolCaseAlias.query.count(), 0)
        confirm = next(option["action_id"] for option in session.context_data["last_options_sent"]
                       if option["action_id"].startswith("education_case_confirm:"))
        self.post(confirm, "Confirmar caso")
        self.assertEqual(PymeTicket.query.count(), 1)
        self.assertEqual(SchoolCaseAlias.query.count(), 1)
        self.assertIn(detail, PymeTicket.query.first().pregunta)
        self.post(confirm, "Confirmar caso")
        self.assertEqual(PymeTicket.query.count(), 1)
        self.assertTrue(self.calls)


if __name__ == "__main__":
    unittest.main()
