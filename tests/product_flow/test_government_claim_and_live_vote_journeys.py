import hashlib
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from sqlalchemy.orm.attributes import flag_modified

os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

from app import create_app, db
from config import Config
from models import (
    ArchivoAdjunto,
    AuditEvent,
    EncRespuesta,
    MessageTemplateRegistry,
    MunicipioTicket,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    User,
    WhatsAppFlowInteraction,
)
from routes.v2.tenants import create_demo_session_token
from services import meta_flow_runtime
from services.meta_flow_media import DownloadedFlowMedia
from socket_service import socketio


class GovernmentProductFlowConfig(Config):
    TESTING = True
    ENABLE_DEMO_MODE = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    EMAIL_NOTIFICATIONS_ENABLED = False
    ENABLE_EMAIL_NOTIFICATIONS = False
    PUBLIC_ENCUESTAS_RATE_LIMIT = 20


class _FixedGovernmentServiceClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 7, 8, 12, 0, 0, tzinfo=tz)


class GovernmentClaimAndLiveVoteJourneysTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(GovernmentProductFlowConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

        self.admin = User(
            name="Gobierno QA",
            email="gobierno-qa@test.com",
            rol="admin",
            tipo_chat="municipio",
            tenant_slug="gobierno-qa",
        )
        self.admin.set_password("secret123")
        db.session.add(self.admin)
        db.session.flush()

        self.tenant = TenantProfile(
            slug="gobierno-qa",
            nombre="Gobierno QA",
            tipo="municipio",
            vertical="gobierno",
            plan="full",
            municipio_id=self.admin.id,
            configuracion={
                "live_chat_schedule": {
                    "enabled": False,
                    "days": [0, 1, 2, 3, 4, 5, 6],
                    "start_time": "09:00",
                    "end_time": "17:00",
                    "timezone": "America/Argentina/Buenos_Aires",
                }
            },
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.admin.tenant_id = self.tenant.id
        self.citizen = User(
            name="Ciudadana estable",
            email="ciudadana-estable@test.com",
            rol="usuario",
            tipo_chat="municipio",
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
        )
        self.citizen.set_password("secret123")
        db.session.add_all([self.admin, self.citizen])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _admin_headers(self):
        token = jwt.encode(
            {
                "user_id": self.admin.id,
                "rol": self.admin.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    def _citizen_headers(self):
        token = jwt.encode(
            {
                "user_id": self.citizen.id,
                "rol": self.citizen.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )
        return {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": self.tenant.slug,
        }

    def _set_live_chat_enabled(self, enabled):
        tenant_config = dict(self.tenant.configuracion or {})
        schedule = dict(tenant_config.get("live_chat_schedule") or {})
        schedule["enabled"] = enabled
        tenant_config["live_chat_schedule"] = schedule
        self.tenant.configuracion = tenant_config
        flag_modified(self.tenant, "configuracion")
        db.session.add(self.tenant)
        db.session.commit()

    def _tracking(self, ticket, request_id):
        return self.client.get(
            "/api/public/tracking/experience",
            query_string={"kind": "claim", "code": f"M-{ticket.nro_ticket}"},
            headers={
                "X-Tracking-Pin": ticket.consulta_pin,
                "X-Request-Id": request_id,
            },
        )

    def _citizen_comment(self, ticket, message, request_id):
        return self.client.post(
            f"/api/public/tracking/claims/{ticket.id}/messages",
            json={"comentario": message},
            headers={
                "X-Tracking-Pin": ticket.consulta_pin,
                "X-Request-Id": request_id,
            },
        )

    def test_government_claim_runs_offline_live_admin_reply_and_tracking(self):
        demo_session_id = create_demo_session_token(
            tenant_slug=self.tenant.slug,
            sector="gobierno",
            rubro="gobierno",
        )

        with patch("services.live_chat_schedule.datetime", _FixedGovernmentServiceClock):
            intake = self.client.post(
                f"/api/ask/municipio?tenant_slug={self.tenant.slug}&demo_session_id={demo_session_id}",
                json={
                    "pregunta": "Hay un bache peligroso frente a la escuela",
                    "demo_mode": True,
                    "tenant_slug": self.tenant.slug,
                    "location": {
                        "lat": -34.6101,
                        "lng": -58.4402,
                        "address": "Escuela 12, San Martin 500",
                    },
                },
                headers={
                    "Origin": "https://www.chatboc.ar",
                    "X-Request-Id": "gov-claim-create-1",
                    "X-Chat-Session-Id": "sid-gov-claim-e2e",
                },
            )

            self.assertEqual(intake.status_code, 200, intake.get_json())
            intake_payload = intake.get_json()
            self.assertEqual(intake_payload["fuente"], "demo_municipio_runtime")
            self.assertEqual(intake_payload["accion_backend"], "demo_crear_reclamo")
            self.assertTrue(intake_payload["result"]["traceable"])
            self.assertEqual(intake_payload["result"]["target"], "public_tracking")
            tracking_href = intake_payload["ticket"]["detail_endpoint"]
            self.assertEqual((intake_payload.get("lead") or {}).get("detail_endpoint"), tracking_href)
            self.assertTrue(
                any(
                    action.get("id") == "track_claim" and action.get("endpoint") == tracking_href
                    for action in intake_payload.get("next_actions") or []
                )
            )
            self.assertNotIn("/api/v2/inbox/omnichannel", str(intake_payload))

            ticket = db.session.get(MunicipioTicket, intake_payload["ticket"]["id"])
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.tenant_id, self.tenant.id)
            self.assertEqual(ticket.municipio_id, self.admin.id)
            self.assertEqual(ticket.categoria, "Baches y calzada")
            self.assertEqual(ticket.direccion, "Escuela 12, San Martin 500")
            self.assertTrue(ticket.consulta_pin)
            self.assertEqual(
                tracking_href,
                f"/tracking/claim/{ticket.nro_ticket}#pin={ticket.consulta_pin}",
            )

            offline_tracking = self._tracking(ticket, "gov-claim-track-offline-1")
            self.assertEqual(offline_tracking.status_code, 200, offline_tracking.get_json())
            offline_tracking_payload = offline_tracking.get_json()
            self.assertEqual(offline_tracking_payload["contract_version"], "tracking.experience.v1")
            self.assertEqual(offline_tracking_payload["resource"]["code"], f"M-{ticket.nro_ticket}")
            self.assertEqual(offline_tracking_payload["support"]["mode"], "offline")
            self.assertFalse(offline_tracking_payload["support"]["socket"]["enabled"])
            self.assertEqual(
                offline_tracking_payload["support"]["cta"]["primary"]["id"],
                "leave_offline_message",
            )

            offline_comment = self._citizen_comment(
                ticket,
                "Dejo foto y referencia para cuando abra la mesa.",
                "gov-claim-comment-offline-1",
            )
            self.assertEqual(offline_comment.status_code, 201, offline_comment.get_json())
            offline_payload = offline_comment.get_json()
            self.assertEqual(offline_payload["contract_version"], "tracking.support_message.v1")
            self.assertEqual(offline_payload["request_id"], "gov-claim-comment-offline-1")
            self.assertEqual(offline_payload["delivery"]["mode"], "offline")
            self.assertTrue(offline_payload["delivery"]["offline_queue"])
            self.assertEqual(offline_payload["delivery"]["reply_status"], "queued_for_agent")
            self.assertEqual(
                offline_payload["tracking"]["support"]["operator_queue"]["state"],
                "offline_waiting_admin_response",
            )

            self._set_live_chat_enabled(True)
            live_tracking = self._tracking(ticket, "gov-claim-track-live-1")
            self.assertEqual(live_tracking.status_code, 200, live_tracking.get_json())
            live_tracking_payload = live_tracking.get_json()
            expected_ticket_room = f"ticket_municipio_{ticket.id}"
            self.assertEqual(live_tracking_payload["support"]["mode"], "live")
            self.assertEqual(live_tracking_payload["support"]["socket"]["room"], expected_ticket_room)
            self.assertTrue(live_tracking_payload["support"]["socket"]["enabled"])
            self.assertTrue(live_tracking_payload["support"]["socket"]["access_token"])
            self.assertEqual(
                live_tracking_payload["support"]["operator_queue"]["state"],
                "live_agent_attention_needed",
            )

            claim_socket_client = socketio.test_client(self.app)
            self.addCleanup(
                lambda: claim_socket_client.disconnect()
                if claim_socket_client.is_connected()
                else None
            )
            self.assertTrue(claim_socket_client.is_connected())
            claim_socket_client.emit(
                "join",
                {
                    "room": expected_ticket_room,
                    "access_token": live_tracking_payload["support"]["socket"]["access_token"],
                },
            )
            join_events = claim_socket_client.get_received()
            join_ack = next(event for event in join_events if event["name"] == "join_ack")
            self.assertEqual(join_ack["args"][0]["room"], expected_ticket_room)

            other_ticket = MunicipioTicket(
                tenant_id=self.tenant.id,
                municipio_id=self.admin.id,
                nro_ticket="760099",
                consulta_pin="731999",
                pregunta="Semaforo intermitente en otra esquina",
                categoria="Transito",
                estado="en_proceso",
            )
            db.session.add(other_ticket)
            db.session.commit()
            other_tracking = self._tracking(other_ticket, "gov-claim-track-other-room-1")
            self.assertEqual(other_tracking.status_code, 200, other_tracking.get_json())
            other_tracking_payload = other_tracking.get_json()
            other_ticket_room = f"ticket_municipio_{other_ticket.id}"
            self.assertEqual(
                other_tracking_payload["support"]["socket"]["room"],
                other_ticket_room,
            )

            other_socket_client = socketio.test_client(self.app)
            self.addCleanup(
                lambda: other_socket_client.disconnect()
                if other_socket_client.is_connected()
                else None
            )
            other_socket_client.emit(
                "join",
                {
                    "room": other_ticket_room,
                    "access_token": other_tracking_payload["support"]["socket"]["access_token"],
                },
            )
            other_join_events = other_socket_client.get_received()
            other_join_ack = next(
                event for event in other_join_events if event["name"] == "join_ack"
            )
            self.assertEqual(other_join_ack["args"][0]["room"], other_ticket_room)

            claim_socket_client.emit(
                "join",
                {
                    "room": other_ticket_room,
                    "access_token": live_tracking_payload["support"]["socket"]["access_token"],
                },
            )
            rejected_join_events = claim_socket_client.get_received()
            rejected_join = next(
                event for event in rejected_join_events if event["name"] == "join_error"
            )
            self.assertEqual(rejected_join["args"][0]["error"], "ticket_room_mismatch")
            self.assertEqual(rejected_join["args"][0]["room"], other_ticket_room)

            live_comment = self._citizen_comment(
                ticket,
                "Ahora estoy conectado; la calle sigue cortada.",
                "gov-claim-comment-live-1",
            )
            self.assertEqual(live_comment.status_code, 201, live_comment.get_json())
            live_payload = live_comment.get_json()
            self.assertEqual(live_payload["delivery"]["mode"], "live")
            self.assertTrue(live_payload["delivery"]["realtime_available"])
            self.assertEqual(live_payload["delivery"]["reply_status"], "sent_to_live_chat")
            self.assertEqual(
                live_payload["tracking"]["support"]["socket"]["room"],
                expected_ticket_room,
            )
            citizen_socket_events = claim_socket_client.get_received()
            citizen_event = next(
                event
                for event in citizen_socket_events
                if event["name"] == "new_chat_message"
                and event["args"][0].get("actor") == "neighbor"
            )
            self.assertEqual(
                citizen_event["args"][0]["mensaje"],
                "Ahora estoy conectado; la calle sigue cortada.",
            )
            self.assertEqual(citizen_event["args"][0]["socket_room"], expected_ticket_room)
            self.assertEqual(other_socket_client.get_received(), [])

            inbox = self.client.get(
                "/api/v2/inbox/omnichannel?limit=20",
                headers={**self._admin_headers(), "X-Request-Id": "gov-claim-inbox-1"},
            )
            self.assertEqual(inbox.status_code, 200, inbox.get_json())
            inbox_item = next(
                item
                for item in inbox.get_json()["items"]
                if item.get("source_model") == "MunicipioTicket" and item.get("legacy_id") == ticket.id
            )
            inbox_messages = [event.get("body") for event in inbox_item["timeline"]]
            self.assertIn("Dejo foto y referencia para cuando abra la mesa.", inbox_messages)
            self.assertIn("Ahora estoy conectado; la calle sigue cortada.", inbox_messages)

            admin_reply = self.client.post(
                "/api/v2/inbox/omnichannel/actions",
                json={
                    "source_model": "MunicipioTicket",
                    "legacy_id": ticket.id,
                    "action": "reply",
                    "body": "La cuadrilla fue asignada y llega dentro de la hora.",
                },
                headers={**self._admin_headers(), "X-Request-Id": "gov-claim-admin-reply-1"},
            )
            self.assertEqual(admin_reply.status_code, 200, admin_reply.get_json())
            admin_payload = admin_reply.get_json()
            self.assertEqual(admin_payload["contract_version"], "inbox.omnichannel.action.v1")
            self.assertTrue(admin_payload["delivery"]["timeline_updated"])
            self.assertEqual(admin_payload["delivery"]["realtime"]["room"], expected_ticket_room)
            self.assertIn("new_chat_message", admin_payload["delivery"]["realtime"]["events"])
            admin_socket_events = claim_socket_client.get_received()
            admin_event = next(
                event
                for event in admin_socket_events
                if event["name"] == "new_chat_message"
                and event["args"][0].get("actor") == "agent"
            )
            self.assertEqual(
                admin_event["args"][0]["mensaje"],
                "La cuadrilla fue asignada y llega dentro de la hora.",
            )
            self.assertEqual(admin_event["args"][0]["socket_room"], expected_ticket_room)

            final_tracking = self._tracking(ticket, "gov-claim-track-after-reply-1")
            self.assertEqual(final_tracking.status_code, 200, final_tracking.get_json())
            final_payload = final_tracking.get_json()

        conversation = final_payload["support"]["conversation"]
        messages_by_text = {item["message"]: item for item in conversation["messages"]}
        self.assertEqual(messages_by_text["Dejo foto y referencia para cuando abra la mesa."]["author"], "customer")
        self.assertEqual(messages_by_text["Ahora estoy conectado; la calle sigue cortada."]["author"], "customer")
        self.assertEqual(messages_by_text["La cuadrilla fue asignada y llega dentro de la hora."]["author"], "team")
        self.assertEqual(final_payload["support"]["socket"]["room"], expected_ticket_room)
        self.assertIn(
            "La cuadrilla fue asignada y llega dentro de la hora.",
            [item.get("message") for item in final_payload["timeline"]],
        )

        queue = final_payload["support"]["operator_queue"]
        self.assertEqual(queue["state"], "up_to_date", queue)
        self.assertFalse(queue["has_pending_customer_message"])
        self.assertEqual(queue["pending_customer_messages"], 0)
        self.assertFalse(conversation["unread_for_team"])

        persisted_comments = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).all()
        self.assertEqual(len(persisted_comments), 4)
        self.assertEqual(sum(1 for comment in persisted_comments if comment.es_admin), 1)

    def test_claim_evidence_completion_reaches_public_tracking_and_admin_inbox(self):
        ticket = MunicipioTicket(
            tenant_id=self.tenant.id,
            municipio_id=self.admin.id,
            nro_ticket="760017",
            consulta_pin="731904",
            pregunta="Luminaria caida sobre la vereda",
            categoria="Alumbrado publico",
            estado="en_proceso",
            nombre_vecino="Ciudadana estable",
            canal_ingreso="whatsapp",
        )
        db.session.add(ticket)
        db.session.flush()

        sender = ProviderSender(
            tenant_id=self.tenant.id,
            channel="whatsapp",
            phone_number="+1555760017",
            sender_id="whatsapp:+1555760017",
            waba_id="waba-government-evidence",
            phone_number_id="phone-government-evidence",
            status="active",
        )
        registry = MessageTemplateRegistry(
            tenant_id=self.tenant.id,
            provider="twilio",
            channel="whatsapp",
            name="government-claim-evidence",
            language="es",
            status="approved",
            content_sid="HXGOVCLAIMEVIDENCE",
            external_template_id="meta-government-claim-evidence",
        )
        db.session.add_all([sender, registry])
        db.session.flush()

        interaction = WhatsAppFlowInteraction(
            tenant_id=self.tenant.id,
            template_registry_id=registry.id,
            provider_sender_id=sender.id,
            flow_id=meta_flow_runtime.CLAIM_EVIDENCE_FLOW_ID,
            meta_flow_id=registry.external_template_id,
            content_sid=registry.content_sid,
            recipient_hash=hashlib.sha256(b"government-evidence-recipient").hexdigest(),
            recipient_hint="***0017",
            token_digest=hashlib.sha256(b"government-evidence-token").hexdigest(),
            idempotency_key="government-claim-evidence-760017",
            status="sent",
            data_contract=["ticket_number", "photos", "documents"],
            metadata_json={
                "claim_context": {
                    "kind": "municipio",
                    "id": str(ticket.id),
                    "ticket_number": "M-760017",
                }
            },
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.session.add(interaction)
        db.session.commit()

        photo_content = b"\xff\xd8\xffgovernment-evidence-photo"
        document_content = b"%PDF-1.7\ngovernment-evidence-document"
        photo_digest = hashlib.sha256(photo_content).hexdigest()
        document_digest = hashlib.sha256(document_content).hexdigest()

        def fake_download(**kwargs):
            self.assertEqual(kwargs["provider_sender"].id, sender.id)
            self.assertEqual(
                [item.media_id for item in kwargs["descriptors"]],
                ["provider-photo-760017", "provider-document-760017"],
            )
            return (
                DownloadedFlowMedia(
                    kind="photo",
                    media_id="provider-photo-760017",
                    file_name="luminaria.jpg",
                    mime_type="image/jpeg",
                    content=photo_content,
                    sha256_hex=photo_digest,
                ),
                DownloadedFlowMedia(
                    kind="document",
                    media_id="provider-document-760017",
                    file_name="acta-luminaria.pdf",
                    mime_type="application/pdf",
                    content=document_content,
                    sha256_hex=document_digest,
                ),
            )

        def fake_store(file_storage, user_id=None):
            return ArchivoAdjunto(
                user_id=user_id,
                filename=file_storage.filename,
                nombre_original=file_storage.filename,
                mime=file_storage.content_type,
                tamano=len(file_storage.stream.getvalue()),
                tipo="chat_adjunto",
                url=f"https://cdn.example.test/evidence/{file_storage.filename}",
            )

        submission = {
            "contract_version": "whatsapp.flow_submission.v1",
            "flow": {
                "id": interaction.flow_id,
                "meta_id": interaction.meta_flow_id,
            },
            "payload": {
                "answers": {
                    "ticket_number": "M-760017",
                    "photos": [
                        {
                            "id": "provider-photo-760017",
                            "file_name": "luminaria.jpg",
                            "mime_type": "image/jpeg",
                            "file_size": len(photo_content),
                            "sha256": photo_digest,
                            "cdn_url": "https://untrusted.example/photo",
                        }
                    ],
                    "documents": [
                        {
                            "id": "provider-document-760017",
                            "file_name": "acta-luminaria.pdf",
                            "mime_type": "application/pdf",
                            "file_size": len(document_content),
                            "sha256": document_digest,
                            "cdn_url": "https://untrusted.example/document",
                        }
                    ],
                }
            },
            "correlation": {
                "interaction_id": interaction.id,
                "tenant_id": self.tenant.id,
                "provider_sender_id": sender.id,
            },
        }

        with patch.object(
            meta_flow_runtime,
            "download_claim_evidence_media",
            side_effect=fake_download,
        ), patch.object(
            meta_flow_runtime,
            "create_attachment_with_thumbnail",
            side_effect=fake_store,
        ):
            completion = meta_flow_runtime.apply_whatsapp_flow_completion(
                tenant_id=self.tenant.id,
                interaction_id=interaction.id,
                submission=submission,
                actor_user_id=self.citizen.id,
                anon_id="+5491112347600",
            )
            db.session.commit()

        self.assertEqual(completion["fuente"], "whatsapp_flow_claim_evidence_completed")
        self.assertEqual(completion["attachment_count"], 2)
        self.assertEqual(
            completion["entity"],
            {"kind": "municipio_ticket", "id": ticket.id},
        )
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=self.tenant.id,
                event_type="whatsapp_flow.claim_evidence.completed",
                resource_id=str(ticket.id),
            ).count(),
            1,
        )

        tracking = self._tracking(ticket, "gov-claim-evidence-tracking-1")
        self.assertEqual(tracking.status_code, 200, tracking.get_json())
        tracking_payload = tracking.get_json()
        self.assertEqual(tracking_payload["request_id"], "gov-claim-evidence-tracking-1")
        public_attachments = tracking_payload["attachments"]
        self.assertEqual(
            {item["name"] for item in public_attachments},
            {"luminaria.jpg", "acta-luminaria.pdf"},
        )
        self.assertEqual({item["source"] for item in public_attachments}, {"whatsapp_flow"})
        self.assertEqual({item["status"] for item in public_attachments}, {"ready"})
        self.assertEqual(
            {item["flow_id"] for item in public_attachments},
            {meta_flow_runtime.CLAIM_EVIDENCE_FLOW_ID},
        )
        self.assertTrue(all("storage_url" not in item for item in public_attachments))
        self.assertNotIn("provider-photo-760017", str(tracking_payload))
        self.assertNotIn("provider-document-760017", str(tracking_payload))
        self.assertNotIn("untrusted.example", str(tracking_payload))

        public_evidence_events = [
            item
            for item in tracking_payload["timeline"]
            if item.get("source") == "whatsapp_flow" and item.get("attachments")
        ]
        self.assertEqual(len(public_evidence_events), 2)

        inbox = self.client.get(
            "/api/v2/inbox/omnichannel?limit=20",
            headers={**self._admin_headers(), "X-Request-Id": "gov-claim-evidence-inbox-1"},
        )
        self.assertEqual(inbox.status_code, 200, inbox.get_json())
        inbox_item = next(
            item
            for item in inbox.get_json()["items"]
            if item.get("source_model") == "MunicipioTicket"
            and item.get("legacy_id") == ticket.id
        )
        self.assertEqual(
            {item["name"] for item in inbox_item["attachments"]},
            {"luminaria.jpg", "acta-luminaria.pdf"},
        )
        admin_evidence_events = [
            item
            for item in inbox_item["timeline"]
            if item.get("origin") == "whatsapp_flow" and item.get("attachments")
        ]
        self.assertEqual(len(admin_evidence_events), 2)
        self.assertEqual(
            {event["attachments"][0]["source"] for event in admin_evidence_events},
            {"whatsapp_flow"},
        )

    @patch.dict(
        os.environ,
        {
            "HUGGINGFACE_ZERO_SHOT_ENABLED": "0",
            "HF_ZERO_SHOT_ENABLED": "0",
        },
    )
    def test_realtime_vote_rejects_same_stable_identity_and_keeps_room_results_coherent(self):
        now = datetime.now(timezone.utc)
        created = self.client.post(
            "/api/v2/surveys",
            json={
                "title": "Prioridad de obra barrial",
                "description": "Votacion realtime de gobierno",
                "survey_type": "votacion",
                "channel": "web",
                "live_vote": True,
                "show_live_results": True,
                "allow_anonymous": False,
                "requiere_identidad": True,
                "uniqueness_policy": "por_usuario",
                "opens_at": (now - timedelta(days=1)).isoformat(),
                "closes_at": (now + timedelta(days=1)).isoformat(),
                "questions": [
                    {
                        "type": "single",
                        "label": "Que obra debe priorizarse?",
                        "required": True,
                        "options": ["Plaza Norte", "Polideportivo"],
                        "order_index": 1,
                    }
                ],
            },
            headers={**self._admin_headers(), "X-Request-Id": "gov-vote-create-1"},
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        created_payload = created.get_json()
        survey_id = created_payload["id"]
        self.assertEqual(created_payload["tenant_id"], self.tenant.id)
        self.assertEqual(created_payload["politica_unicidad"], "por_usuario")
        self.assertTrue(created_payload["requiere_identidad"])
        self.assertFalse(created_payload["anonimo_permitido"])
        self.assertTrue(created_payload["es_votacion_envivo"])

        published = self.client.post(
            f"/api/v2/surveys/{survey_id}/publish",
            headers={**self._admin_headers(), "X-Request-Id": "gov-vote-publish-1"},
        )
        self.assertEqual(published.status_code, 200, published.get_json())
        published_payload = published.get_json()
        public_token = published_payload["public_token"]
        expected_room = f"encuesta:{self.tenant.slug}:{public_token}"
        self.assertEqual(published_payload["public_state"]["status"], "live")
        self.assertEqual(published_payload["realtime"]["room"], expected_room)

        public = self.client.get(published_payload["links"]["public_api_endpoint"])
        self.assertEqual(public.status_code, 200, public.get_json())
        public_payload = public.get_json()
        self.assertEqual(public_payload["contract_version"], "surveys.public.v2")
        self.assertEqual(public_payload["politica_unicidad"], "por_usuario")
        self.assertTrue(public_payload["requiere_identidad"])
        self.assertFalse(public_payload["anonimo_permitido"])
        self.assertEqual(public_payload["realtime"]["room"], expected_room)

        socket_client = socketio.test_client(self.app)
        self.addCleanup(
            lambda: socket_client.disconnect() if socket_client.is_connected() else None
        )
        self.assertTrue(socket_client.is_connected())
        socket_client.emit("join", {"room": expected_room})
        socket_client.get_received()

        question = public_payload["preguntas"][0]
        first_option = question["opciones"][0]
        second_option = question["opciones"][1]
        first_submission_id = "gov-vote-first-submission-0001"
        first_vote_body = {
            "submission_id": first_submission_id,
            "user_id": self.citizen.id,
            "anon_id": "browser-installation-a",
            "source": "web",
            "respuestas": [
                {"pregunta_id": question["id"], "opcion_id": first_option["id"]}
            ],
        }
        first_vote = self.client.post(
            published_payload["links"]["respond_endpoint"],
            json=first_vote_body,
            headers={
                **self._citizen_headers(),
                "Idempotency-Key": first_submission_id,
                "X-Forwarded-For": "203.0.113.41",
                "X-Request-Id": "gov-vote-first-1",
            },
        )
        self.assertEqual(first_vote.status_code, 201, first_vote.get_json())
        first_ack = first_vote.get_json()
        self.assertTrue(first_ack["ok"])
        self.assertEqual(first_ack["contract_version"], "surveys.public_response.v2")
        self.assertFalse(first_ack["replayed"])
        self.assertEqual(first_ack["idempotency"]["submission_id"], first_submission_id)
        self.assertEqual(first_ack["idempotency"]["disposition"], "accepted")
        self.assertEqual(first_ack["realtime"]["room"], expected_room)
        self.assertEqual(first_ack["realtime"]["socket"]["join_payload"], {"room": expected_room})
        realtime_events = socket_client.get_received()
        realtime_event_names = {event["name"] for event in realtime_events}
        self.assertIn("survey_update_v2", realtime_event_names)
        self.assertIn("survey.vote.created", realtime_event_names)
        live_event = next(
            event for event in realtime_events if event["name"] == "survey_update_v2"
        )
        self.assertEqual(live_event["args"][0]["contract_version"], "surveys.live_results.v2")
        self.assertEqual(live_event["args"][0]["total_respuestas"], 1)

        replay = self.client.post(
            published_payload["links"]["respond_endpoint"],
            json=first_vote_body,
            headers={
                **self._citizen_headers(),
                "Idempotency-Key": first_submission_id,
                "X-Forwarded-For": "203.0.113.41",
                "X-Request-Id": "gov-vote-first-replay-1",
            },
        )
        self.assertEqual(replay.status_code, 200, replay.get_json())
        replay_ack = replay.get_json()
        self.assertTrue(replay_ack["replayed"])
        self.assertEqual(replay_ack["response_id"], first_ack["response_id"])
        self.assertEqual(replay_ack["idempotency"]["submission_id"], first_submission_id)
        self.assertEqual(replay_ack["idempotency"]["disposition"], "replayed")
        self.assertEqual(socket_client.get_received(), [])

        stored_response = EncRespuesta.query.filter_by(encuesta_id=survey_id).one()
        self.assertEqual(stored_response.user_id, self.citizen.id)
        self.assertEqual(len(stored_response.huella_unica), 64)
        self.assertRegex(stored_response.huella_unica, r"^[0-9a-f]{64}$")
        self.assertNotEqual(stored_response.huella_unica, str(self.citizen.id))

        duplicate_submission_id = "gov-vote-duplicate-submission-0001"
        duplicate_vote = self.client.post(
            published_payload["links"]["respond_endpoint"],
            json={
                "submission_id": duplicate_submission_id,
                "user_id": self.citizen.id,
                "anon_id": "browser-installation-b",
                "source": "whatsapp_webview",
                "respuestas": [
                    {"pregunta_id": question["id"], "opcion_id": second_option["id"]}
                ],
            },
            headers={
                **self._citizen_headers(),
                "Idempotency-Key": duplicate_submission_id,
                "X-Forwarded-For": "198.51.100.92",
                "X-Request-Id": "gov-vote-duplicate-1",
            },
        )
        self.assertEqual(duplicate_vote.status_code, 409, duplicate_vote.get_json())
        duplicate_payload = duplicate_vote.get_json()
        self.assertEqual(duplicate_payload["status_code"], 409)
        self.assertEqual(duplicate_payload["reason_code"], "survey_response_duplicate")
        self.assertFalse(duplicate_payload["retryable"])
        self.assertIn("participaci", duplicate_payload["message"].lower())
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 1)
        self.assertEqual(socket_client.get_received(), [])

        live_results = self.client.get(first_ack["live_results_url"])
        self.assertEqual(live_results.status_code, 200, live_results.get_json())
        results_payload = live_results.get_json()
        self.assertEqual(results_payload["contract_version"], "surveys.live_results.v2")
        self.assertEqual(results_payload["total_respuestas"], 1)
        self.assertEqual(results_payload["preguntas"][0]["total_votos"], 1)
        option_results = {
            option["id"]: option["votos"]
            for option in results_payload["preguntas"][0]["opciones"]
        }
        self.assertEqual(option_results[first_option["id"]], 1)
        self.assertEqual(option_results[second_option["id"]], 0)
        self.assertEqual(results_payload["realtime"]["room"], expected_room)
        self.assertEqual(results_payload["realtime"]["rooms"], [expected_room])
        self.assertEqual(
            results_payload["realtime"]["socket"]["join_payload"],
            {"room": expected_room},
        )
        self.assertEqual(
            results_payload["realtime"]["polling"]["href"],
            first_ack["live_results_url"],
        )

        late_citizen = User(
            name="Ciudadano fuera de ventana",
            email="ciudadano-fuera-de-ventana@test.com",
            rol="usuario",
            tipo_chat="municipio",
            tenant_id=self.tenant.id,
            tenant_slug=self.tenant.slug,
        )
        late_citizen.set_password("secret123")
        db.session.add(late_citizen)
        db.session.commit()
        late_token = jwt.encode(
            {
                "user_id": late_citizen.id,
                "rol": late_citizen.rol,
                "tenant_slug": self.tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            self.app.config["SECRET_KEY"],
            algorithm="HS256",
        )

        closed = self.client.post(
            f"/api/v2/surveys/{survey_id}/close",
            headers={**self._admin_headers(), "X-Request-Id": "gov-vote-close-1"},
        )
        self.assertEqual(closed.status_code, 200, closed.get_json())
        self.assertEqual(closed.get_json()["estado"], "cerrada")
        socket_client.get_received()

        late_submission_id = "gov-vote-after-close-submission-0001"
        late_vote = self.client.post(
            published_payload["links"]["respond_endpoint"],
            json={
                "submission_id": late_submission_id,
                "user_id": late_citizen.id,
                "source": "web",
                "respuestas": [
                    {"pregunta_id": question["id"], "opcion_id": second_option["id"]}
                ],
            },
            headers={
                "Authorization": f"Bearer {late_token}",
                "Idempotency-Key": late_submission_id,
                "X-Tenant-Slug": self.tenant.slug,
                "X-Forwarded-For": "198.51.100.93",
                "X-Request-Id": "gov-vote-after-close-1",
            },
        )
        self.assertEqual(late_vote.status_code, 403, late_vote.get_json())
        self.assertEqual(late_vote.get_json()["reason_code"], "survey_not_published")
        self.assertEqual(EncRespuesta.query.filter_by(encuesta_id=survey_id).count(), 1)
        self.assertEqual(socket_client.get_received(), [])

        closed_results = self.client.get(first_ack["live_results_url"])
        self.assertEqual(closed_results.status_code, 403, closed_results.get_json())
        self.assertEqual(closed_results.get_json()["reason_code"], "survey_not_published")


if __name__ == "__main__":
    unittest.main()
