import os
import unittest
from unittest.mock import patch

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")

from app import create_app, db
from config import Config
from models import ChatSessionContext, PymeTicket, TenantProfile, User
from models_education import Campus, School, SchoolCaseAlias
from services.pymes import responder_pyme
from services.ticket_service import servicio_tickets


class EducationWidgetConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False


class EducationWidgetFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(EducationWidgetConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.owner = User(name="Colegio Demo", email="colegio-widget@test.com", password_hash="hash", tipo_chat="pyme")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="qa-colegio-sandbox",
            nombre="QA Colegio Sandbox",
            tipo="pyme",
            pyme_id=self.owner.id,
            plan="full",
            vertical="educacion",
            subvertical="colegio_general",
            capabilities_json={"education": {"enabled": True}},
            is_active=True,
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.school = School(
            tenant_id=self.tenant.id,
            name="QA Colegio Sandbox",
            status="active",
        )
        db.session.add(self.school)
        db.session.flush()
        db.session.add(
            Campus(
                school_id=self.school.id,
                name="Sede principal",
                is_main=True,
            )
        )
        db.session.flush()
        self.session_context = ChatSessionContext(
            chat_session_id="edu-widget-session",
            tenant_id=self.tenant.id,
            anon_id="anon-edu",
            context_data={},
        )
        db.session.add(self.session_context)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_create_school_case_action_prompts_for_detail(self):
        response = responder_pyme(
            {
                "pregunta": "",
                "action_id": "create_school_case",
                "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
            },
            self.owner,
            None,
            chat_db_context=self.session_context,
            anon_id="anon-edu",
            channel="widget",
        )

        self.assertEqual(response.get("fuente"), "education_widget_case_prompt")
        self.assertIn("tramite", response.get("message_body", "").lower())
        pending = self.session_context.context_data.get("education_pending_case")
        self.assertIsInstance(pending, dict)
        self.assertEqual(pending.get("intent"), "tramites_secretaria")

    def test_menu_colegio_uses_fixed_audio_cache_contract(self):
        response = responder_pyme(
            {
                "pregunta": "",
                "action_id": "menu_colegio",
                "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
            },
            self.owner,
            None,
            chat_db_context=self.session_context,
            anon_id="anon-edu",
            channel="widget",
        )

        self.assertEqual(response.get("fuente"), "education_widget_menu")
        self.assertTrue(response.get("generar_audio"))
        self.assertTrue(response.get("menu_audio_enabled"))
        self.assertTrue(response.get("audio_text"))
        self.assertEqual(response.get("tts_cache_text"), response.get("audio_text"))
        self.assertEqual(
            response.get("tts_cache_namespace"),
            "whatsapp:menu:qa-colegio-sandbox:menu-colegio:widget:reduced:v1",
        )
        self.assertEqual(
            response.get("audio_cache_policy"),
            {
                "kind": "fixed_menu",
                "scope": "tenant",
                "cache": "tts_audio_cache",
                "inclusive": True,
            },
        )
        self.assertIn("Opcion 1", response.get("audio_text", ""))
        self.assertNotIn("anon-edu", response.get("tts_cache_text", ""))

    def test_pending_school_case_detail_creates_ticket(self):
        responder_pyme(
            {
                "pregunta": "",
                "action_id": "justify_absence",
                "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
            },
            self.owner,
            None,
            chat_db_context=self.session_context,
            anon_id="anon-edu",
            channel="widget",
        )

        with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
            response = responder_pyme(
                {
                    "pregunta": "Mi hija Sofia Perez de 3A falto hoy por fiebre.",
                    "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
                },
                self.owner,
                None,
                chat_db_context=self.session_context,
                anon_id="anon-edu",
                channel="widget",
            )

        self.assertEqual(response.get("fuente"), "education_widget_case_created")
        self.assertEqual(PymeTicket.query.count(), 1)
        ticket = PymeTicket.query.first()
        self.assertEqual(ticket.tenant_id, self.tenant.id)
        self.assertEqual(ticket.categoria, "inasistencia")
        self.assertEqual(SchoolCaseAlias.query.count(), 1)
        self.assertNotIn("education_pending_case", self.session_context.context_data)

    def test_talk_secretary_creates_live_handoff_ticket_with_school_alias(self):
        with patch.object(servicio_tickets, "_notificar_ticket_por_email", return_value=None):
            response = responder_pyme(
                {
                    "pregunta": "",
                    "action_id": "talk_secretary",
                    "education_context": {"is_education": True, "tenant_slug": self.tenant.slug},
                },
                self.owner,
                None,
                chat_db_context=self.session_context,
                anon_id="anon-edu",
                channel="widget",
            )

        self.assertEqual(response.get("fuente"), "education_widget_live_handoff")
        self.assertTrue(response.get("request_id"))
        data = response.get("data") or {}
        self.assertEqual(data.get("status"), "esperando_agente_en_vivo")
        self.assertIsInstance(data.get("live_chat"), dict)
        self.assertTrue(data.get("live_chat_access_token"))
        self.assertTrue(str(data.get("socket_room") or "").startswith("ticket_pyme_"))
        self.assertEqual(data["live_chat"].get("access_mode"), "signed_ticket_room")
        self.assertIsInstance(data.get("school_case"), dict)
        self.assertEqual(PymeTicket.query.count(), 1)
        self.assertEqual(PymeTicket.query.first().estado, "esperando_agente_en_vivo")
        self.assertEqual(SchoolCaseAlias.query.count(), 1)


if __name__ == "__main__":
    unittest.main()
