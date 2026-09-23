import json
import unittest
from datetime import timedelta

from app import create_app, db
from config import TestConfig
from models import CategoriaTicket, MunicipioTicket, TenantProfile, User
from routes.ticket import serialize_ticket_to_json, _serialize_ticket_details
from utils.time_utils import get_local_now


class TicketOperationalBadgesTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        self.admin = User(name="Admin", email="ops@example.com", rol="admin", tipo_chat="municipio")
        self.admin.set_password("pass")
        db.session.add(self.admin)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _tenant(self, slug: str):
        owner = User(name=slug, email=f"{slug}@example.com", rol="admin", tipo_chat="municipio")
        owner.set_password("pass")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(slug=slug, nombre=slug, tipo="municipio", municipio_id=owner.id)
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        return owner, tenant

    def test_compact_payload_prefers_tenant_catalog_category_and_reports_conflict(self):
        owner, tenant = self._tenant("category-authority")
        category = CategoriaTicket(nombre="Luminarias", tenant_id=tenant.id)
        db.session.add(category)
        db.session.flush()
        ticket = MunicipioTicket(
            tenant_id=tenant.id, municipio_id=owner.id, nro_ticket="M-AUTH-1",
            pregunta="Caso", categoria="General", categoria_id=category.id,
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(payload["categoria"], "Luminarias")
        self.assertEqual(payload["categoria_id"], category.id)
        self.assertEqual(payload["authoritative_category"], "Luminarias")
        self.assertTrue(payload["category_authority"]["verified"])
        self.assertTrue(payload["category_authority"]["conflict"])
        self.assertEqual(payload["category_authority"]["source"], "tenant_category_catalog")

    def test_detail_publishes_canonical_alias_without_inferring_from_subject(self):
        owner, tenant = self._tenant("category-detail")
        ticket = MunicipioTicket(
            tenant_id=tenant.id, municipio_id=owner.id, nro_ticket="M-AUTH-2",
            pregunta="Caso", asunto="Texto no autoritativo", categoria="alumbrado publico",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = _serialize_ticket_details(ticket, "municipio")

        self.assertEqual(payload["categoria_reclamo"], "Luminarias")
        self.assertEqual(payload["categoria"], "Luminarias")
        self.assertIsNone(payload["categoria_id"])
        self.assertEqual(payload["authoritative_category"], "Luminarias")
        self.assertEqual(payload["category_authority"]["source"], "persisted_category_exact_alias")

    def test_missing_or_foreign_category_id_fails_closed_to_persisted_category(self):
        owner, tenant = self._tenant("category-local")
        _, foreign_tenant = self._tenant("category-foreign")
        foreign = CategoriaTicket(nombre="Luminarias", tenant_id=foreign_tenant.id)
        db.session.add(foreign)
        db.session.flush()
        tickets = [
            MunicipioTicket(
                tenant_id=tenant.id, municipio_id=owner.id, nro_ticket="M-AUTH-3",
                pregunta="Caso", asunto="Alumbrado en el titulo", categoria="General",
            ),
            MunicipioTicket(
                tenant_id=tenant.id, municipio_id=owner.id, nro_ticket="M-AUTH-4",
                pregunta="Caso", categoria="General", categoria_id=foreign.id,
            ),
        ]
        db.session.add_all(tickets)
        db.session.commit()

        for ticket in tickets:
            payload = serialize_ticket_to_json(ticket, "municipio", compact=True)
            self.assertEqual(payload["categoria"], "General")
            self.assertIsNone(payload["authoritative_category"])
            self.assertFalse(payload["category_authority"]["verified"])

    def test_unassigned_ticket_exposes_sla_badges(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="bache",
            nro_ticket="123456",
            estado="nuevo",
            fecha=get_local_now() - timedelta(hours=9),
            ultima_actividad=get_local_now() - timedelta(hours=9),
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio")

        self.assertEqual(payload["sla_status"], "por_vencer")
        self.assertIn("sin_asignar", payload["operational_badges"])
        self.assertIn("por_vencer", payload["operational_badges"])
        self.assertEqual(payload["crm_queue"]["contract_version"], "tickets.crm_queue.v1")
        self.assertEqual(payload["crm_queue"]["state"], "sla_attention")
        self.assertEqual(payload["crm_queue"]["next_team_action"], "review_sla_and_update")
        self.assertTrue(payload["crm_queue"]["requires_admin_response"])
        self.assertIn(
            {"id": "sla_risk", "label": "SLA en riesgo", "tone": "warning"},
            payload["crm_queue"]["badges"],
        )
        self.assertIn(
            {"id": "unassigned", "label": "Sin responsable", "tone": "warning"},
            payload["crm_queue"]["badges"],
        )

    def test_ticket_payload_does_not_expose_runtime_json_as_case_summary(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="La luminaria de la plaza no enciende desde anoche.",
            asunto="Demo reclamo - Alumbrado publico",
            categoria="Luminarias",
            nro_ticket="419",
            estado="nuevo",
            detalles=json.dumps(
                {
                    "demo_runtime": True,
                    "source": "demo_municipio_runtime",
                    "chat_session_id": "internal-session-id",
                    "demo_session_payload": {"tenant_slug": "junin"},
                }
            ),
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio")

        self.assertEqual(
            payload["description"],
            "La luminaria de la plaza no enciende desde anoche.",
        )
        self.assertNotIn("demo_runtime", payload["description"])
        self.assertNotIn("chat_session_id", payload["description"])

    def test_ticket_payload_uses_explicit_human_summary_from_structured_details(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="Necesito ayuda",
            asunto="Consulta ciudadana",
            categoria="Atencion",
            nro_ticket="420",
            estado="nuevo",
            detalles=json.dumps(
                {
                    "summary": "Vecino solicita orientación para completar el trámite.",
                    "source": "assisted_intake",
                }
            ),
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(
            payload["description"],
            "Vecino solicita orientación para completar el trámite.",
        )

    def test_ticket_payload_preserves_legacy_plain_text_details(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="Consulta original",
            categoria="Arbolado",
            nro_ticket="421",
            estado="nuevo",
            detalles="Árbol caído sobre la vereda, sin cables comprometidos.",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(
            payload["description"],
            "Árbol caído sobre la vereda, sin cables comprometidos.",
        )

    def test_ticket_payload_rejects_malformed_json_looking_details(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="Bache peligroso frente a la escuela.",
            categoria="Calles",
            nro_ticket="422",
            estado="nuevo",
            detalles='{"demo_runtime": true, "chat_session_id":',
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(
            payload["description"],
            "Bache peligroso frente a la escuela.",
        )
        self.assertNotIn("demo_runtime", payload["description"])

    def test_ticket_queue_contract_prioritizes_unread_customer_activity(self):
        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="consulta de estado",
            nro_ticket="123457",
            estado="nuevo",
            fecha=get_local_now(),
            ultima_actividad=get_local_now(),
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(
            ticket,
            "municipio",
            compact=True,
            collaboration_state_override={
                "unread_count": 2,
                "unread_viewer_count": 1,
                "has_unread": True,
                "active_viewers_count": 1,
            },
        )

        self.assertEqual(payload["crm_queue"]["state"], "customer_waiting")
        self.assertEqual(payload["crm_queue"]["label"], "Responder ahora")
        self.assertEqual(payload["crm_queue"]["next_team_action"], "reply_from_crm")
        self.assertEqual(payload["crm_queue"]["signals"]["unread_count"], 2)
        self.assertEqual(payload["crm_queue"]["signals"]["active_viewers_count"], 1)
        self.assertGreaterEqual(payload["crm_queue"]["score"], 100)
        self.assertIn(
            {"id": "unread", "label": "Mensaje sin leer", "tone": "live"},
            payload["crm_queue"]["badges"],
        )

    def test_detail_payload_marks_pending_response_for_assigned_ticket(self):
        agente = User(
            name="Agente",
            email="agente@example.com",
            rol="empleado",
            tipo_chat="municipio",
            municipio_id=self.admin.id,
        )
        agente.set_password("pass")
        db.session.add(agente)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="luminaria",
            nro_ticket="654321",
            estado="en_proceso",
            fecha=get_local_now() - timedelta(hours=12),
            ultima_actividad=get_local_now() - timedelta(hours=3),
            asignado_a_id=agente.id,
        )
        db.session.add(ticket)
        db.session.commit()

        payload = _serialize_ticket_details(ticket, "municipio")

        self.assertEqual(payload["sla_status"], "seguimiento")
        self.assertIn("respuesta_pendiente", payload["operational_badges"])
        self.assertGreaterEqual(payload["operational_metrics"]["inactivity_hours"], 3)

    def test_ticket_payload_exposes_only_consented_profile_avatar(self):
        vecino = User(
            name="Marcelo Vecino",
            email="vecino-avatar@example.com",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            user_id=vecino.id,
            pregunta="arreglo de calle",
            nro_ticket="777001",
            estado="nuevo",
            nombre_vecino="Marcelo Vecino",
            telefono_vecino="+5492613168608",
        )
        db.session.add(ticket)
        db.session.commit()

        list_payload = serialize_ticket_to_json(ticket, "municipio", compact=True)
        detail_payload = _serialize_ticket_details(ticket, "municipio")

        self.assertEqual(list_payload["avatar_url"], "https://cdn.example.com/profile/vecino.webp")
        self.assertTrue(list_payload["avatar_consent"])
        self.assertEqual(list_payload["contact_identity"]["avatar_source"], "profile_upload")
        self.assertEqual(list_payload["contact"]["avatar_url"], "https://cdn.example.com/profile/vecino.webp")
        self.assertEqual(
            detail_payload["informacion_personal_vecino"]["avatar_url"],
            "https://cdn.example.com/profile/vecino.webp",
        )
        self.assertEqual(
            detail_payload["contact"]["identity"]["fallback"],
            "deterministic_identity_avatar",
        )

    def test_ticket_payload_hides_untrusted_whatsapp_profile_avatar(self):
        vecino = User(
            name="WhatsApp Vecino",
            email="vecino-whatsapp-avatar@example.com",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/wa-profile.webp",
                "avatar_source": "whatsapp_profile",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            user_id=vecino.id,
            pregunta="luminaria",
            nro_ticket="777002",
            estado="nuevo",
            nombre_vecino="WhatsApp Vecino",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertIsNone(payload["avatar_url"])
        self.assertFalse(payload["avatar_consent"])
        self.assertIsNone(payload["contact_identity"]["avatar_url"])
        self.assertEqual(payload["contact_identity"]["fallback"], "deterministic_identity_avatar")

    def test_ticket_payload_resolves_registered_profile_avatar_by_email_without_user_id(self):
        vecino = User(
            name="Vecino Registrado",
            email="vecino-registrado@example.com",
            telefono="+5492613168608",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino-registrado.webp",
                "avatar_source": "social_login_google",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="arreglo de calle",
            nro_ticket="777003",
            estado="nuevo",
            nombre_vecino="Vecino Registrado",
            telefono_vecino="+54 9 261 316 8608",
            email_vecino="vecino-registrado@example.com",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(payload["avatar_url"], "https://cdn.example.com/profile/vecino-registrado.webp")
        self.assertTrue(payload["avatar_consent"])
        self.assertEqual(payload["contact_identity"]["user_id"], vecino.id)
        self.assertEqual(payload["contact_identity"]["avatar_source"], "social_login_google")
        self.assertEqual(payload["contact"]["avatar_url"], "https://cdn.example.com/profile/vecino-registrado.webp")

    def test_ticket_payload_resolves_registered_profile_avatar_by_phone_without_user_id(self):
        vecino = User(
            name="Vecino Telefono",
            email="vecino-telefono@example.com",
            telefono="+5492613000000",
            rol="user",
            tipo_chat="municipio",
        )
        vecino.set_password("pass")
        vecino.accesibilidad = {
            "identity": {
                "avatar_url": "https://cdn.example.com/profile/vecino-telefono.webp",
                "avatar_source": "profile_upload",
                "avatar_consent": True,
            }
        }
        db.session.add(vecino)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=self.admin.id,
            pregunta="luminaria",
            nro_ticket="777004",
            estado="nuevo",
            nombre_vecino="Vecino Telefono",
            telefono_vecino="5492613000000",
        )
        db.session.add(ticket)
        db.session.commit()

        payload = serialize_ticket_to_json(ticket, "municipio", compact=True)

        self.assertEqual(payload["avatar_url"], "https://cdn.example.com/profile/vecino-telefono.webp")
        self.assertTrue(payload["avatar_consent"])
        self.assertEqual(payload["contact_identity"]["user_id"], vecino.id)
        self.assertEqual(payload["contact_identity"]["avatar_source"], "profile_upload")


if __name__ == "__main__":
    unittest.main()
