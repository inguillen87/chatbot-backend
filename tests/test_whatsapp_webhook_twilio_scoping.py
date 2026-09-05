import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SKIP_INIT_TENANTS", "1")
os.environ.setdefault("OPENAI_API_KEY", "test")

from twilio.request_validator import RequestValidator

from app import create_app
from config import Config
from extensions import db
from models import (
    AuditEvent,
    MessagingEventLedger,
    MunicipioTicket,
    Notification,
    NotificationAttempt,
    ProviderSender,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    TenantTicketReplyEvent,
    User,
    WhatsappNumero,
)
from routes import whatsapp_webhook as webhook_module


PARENT_ACCOUNT_SID = "ACparent_scoped_test"
PARENT_AUTH_TOKEN = "parent-scoped-secret"
CHILD_ACCOUNT_SID = "ACchild_scoped_test"
CHILD_AUTH_TOKEN = "child-scoped-secret"
CHILD_TOKEN_REF = "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACCHILD_SCOPED_TEST"
MESSAGING_SERVICE_SID = "MG_scoped_test"
SENDER_NUMBER = "+15550102030"
RECIPIENT_NUMBER = "+15550908070"


class ScopedTwilioConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    TWILIO_ACCOUNT_SID = PARENT_ACCOUNT_SID
    TWILIO_AUTH_TOKEN = PARENT_AUTH_TOKEN
    TWILIO_ALLOW_NETWORK_IN_TESTS = True
    BACKEND_URL = "http://localhost"
    PUBLIC_API_BASE_URL = "http://localhost"
    WHATSAPP_AUDIO_ENABLED = False


class TwilioWebhookCredentialScopingTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(ScopedTwilioConfig)
        self.app.config[CHILD_TOKEN_REF] = CHILD_AUTH_TOKEN
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.parent_client = MagicMock()
        self.parent_client.messages.create.return_value = SimpleNamespace(sid="SM_parent_reply")
        self.validator_patch = patch.object(
            webhook_module,
            "validator",
            RequestValidator(PARENT_AUTH_TOKEN),
        )
        self.parent_client_patch = patch.object(
            webhook_module,
            "twilio_client",
            self.parent_client,
        )
        self.validator_patch.start()
        self.parent_client_patch.start()

    def tearDown(self):
        self.parent_client_patch.stop()
        self.validator_patch.stop()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def _create_sender(
        self,
        *,
        child_scoped: bool,
        create_provider_sender: bool = True,
    ) -> ProviderSender | None:
        owner = User(
            name="Scoped Tenant",
            email=f"scoped-{int(child_scoped)}@example.com",
            rol="empresa",
            tipo_chat="pyme",
            nombre_empresa="Scoped Tenant",
        )
        owner.set_password("test-password")
        db.session.add(owner)
        db.session.flush()

        state = {}
        if child_scoped:
            state = {
                "twilio_account_sid": CHILD_ACCOUNT_SID,
                "twilio_subaccount_token_ref": CHILD_TOKEN_REF,
                "messaging_service_sid": MESSAGING_SERVICE_SID,
            }
        tenant = TenantProfile(
            slug=f"scoped-tenant-{int(child_scoped)}",
            nombre="Scoped Tenant",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={"twilio_tech_provider": state} if state else {},
        )
        db.session.add(tenant)
        db.session.flush()

        sender = None
        if create_provider_sender:
            sender = ProviderSender(
                tenant_id=tenant.id,
                channel="whatsapp",
                phone_number=SENDER_NUMBER,
                sender_id=f"whatsapp:{SENDER_NUMBER}",
                messaging_service_sid=MESSAGING_SERVICE_SID,
                status="active",
            )
        mapping = WhatsappNumero(
            numero_whatsapp=SENDER_NUMBER,
            user_id=owner.id,
            is_active=True,
        )
        db.session.add(mapping)
        if sender:
            db.session.add(sender)
        db.session.commit()
        return sender

    @staticmethod
    def _signature(path: str, payload: dict, token: str) -> str:
        return RequestValidator(token).compute_signature(f"http://localhost{path}", payload)

    @staticmethod
    def _bot_response() -> dict:
        return {
            "message_body": "Respuesta del tenant.",
            "message_type": "text",
            "options_list": [],
        }

    def test_child_signature_is_accepted_and_reply_uses_child_client(self):
        self._create_sender(child_scoped=True)
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "Necesito informacion",
            "MessageSid": "SM_child_inbound",
        }
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SM_child_reply")

        with (
            patch.object(webhook_module, "Client", return_value=child_client) as client_factory,
            patch.object(webhook_module, "responder_chatboc", return_value=self._bot_response()),
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        CHILD_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        client_factory.assert_called_once_with(CHILD_ACCOUNT_SID, CHILD_AUTH_TOKEN)
        child_client.messages.create.assert_called()
        self.parent_client.messages.create.assert_not_called()

    def test_child_media_download_uses_child_account_credentials(self):
        self._create_sender(child_scoped=True)
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "",
            "MessageSid": "SM_child_media_inbound",
            "MediaMessageSid": "MM_child_media_inbound",
            "MediaUrl0": "https://api.twilio.com/media/private-audio",
            "MediaContentType0": "audio/ogg",
        }
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SM_child_media_reply")
        media_response = MagicMock(content=b"tenant scoped audio")
        media_response.raise_for_status.return_value = None
        attachment = SimpleNamespace(
            id=501,
            url="https://cdn.example.test/audio.ogg",
            mime="audio/ogg",
            nombre_original="audio.ogg",
            analisis=None,
        )

        with (
            patch.object(webhook_module, "Client", return_value=child_client),
            patch.object(webhook_module, "responder_chatboc", return_value=self._bot_response()),
            patch.object(webhook_module.requests, "get", return_value=media_response) as media_get,
            patch.object(webhook_module, "create_attachment_with_thumbnail", return_value=attachment),
            patch.object(webhook_module, "create_whatsapp_assisted_intake", return_value=None),
            patch(
                "services.audio_transcription_service.transcribe_audio_bytes",
                return_value="Necesito informacion",
            ),
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        CHILD_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        media_get.assert_called_once()
        self.assertEqual(
            media_get.call_args.kwargs["auth"],
            (CHILD_ACCOUNT_SID, CHILD_AUTH_TOKEN),
        )
        self.assertTrue(media_get.call_args.kwargs["stream"])
        media_response.close.assert_called_once_with()
        self.parent_client.messages.create.assert_not_called()

    def test_oversize_chunked_media_is_closed_and_never_persisted(self):
        self._create_sender(child_scoped=True)
        self.app.config["WHATSAPP_MEDIA_MAX_BYTES"] = 1024
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "Archivo adjunto",
            "MessageSid": "SM_child_media_oversize",
            "MediaMessageSid": "MM_child_media_oversize",
            "MediaUrl0": "https://api.twilio.com/media/oversize",
            "MediaContentType0": "audio/ogg",
        }
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SM_child_reply")
        media_response = MagicMock()
        media_response.headers = {}
        media_response.raise_for_status.return_value = None
        media_response.iter_content.return_value = [b"1" * 800, b"2" * 300]

        with (
            patch.object(webhook_module, "Client", return_value=child_client),
            patch.object(webhook_module, "responder_chatboc", return_value=self._bot_response()),
            patch.object(webhook_module.requests, "get", return_value=media_response),
            patch.object(webhook_module, "create_attachment_with_thumbnail") as create_attachment,
            patch(
                "services.audio_transcription_service.transcribe_audio_bytes"
            ) as transcribe_audio,
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        CHILD_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        media_response.close.assert_called_once_with()
        create_attachment.assert_not_called()
        transcribe_audio.assert_not_called()

    def test_failed_audio_download_replies_honestly_once_and_never_calls_the_bot(self):
        self._create_sender(child_scoped=True)
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "",
            "MessageSid": "SMaudiofailure001",
            "MediaMessageSid": "MMaudiofailure001",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/unavailable-audio",
            "MediaContentType0": "audio/ogg; codecs=opus",
        }
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SMhonestfallback001")

        with (
            patch.object(webhook_module, "Client", return_value=child_client),
            patch.object(
                webhook_module.requests,
                "get",
                side_effect=webhook_module.requests.exceptions.RequestException("provider unavailable"),
            ) as media_get,
            patch.object(webhook_module, "create_attachment_with_thumbnail") as create_attachment,
            patch.object(webhook_module, "create_whatsapp_assisted_intake") as assisted_intake,
            patch.object(webhook_module, "responder_chatboc") as responder,
        ):
            headers = {
                "X-Twilio-Signature": self._signature(
                    "/webhook/whatsapp",
                    payload,
                    CHILD_AUTH_TOKEN,
                )
            }
            first = self.client.post("/webhook/whatsapp", data=payload, headers=headers)
            duplicate = self.client.post("/webhook/whatsapp", data=payload, headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(media_get.call_count, 1)
        create_attachment.assert_not_called()
        assisted_intake.assert_not_called()
        responder.assert_not_called()
        child_client.messages.create.assert_called_once()
        reply = child_client.messages.create.call_args.kwargs["body"]
        self.assertIn("no pude", reply.lower())
        self.assertIn("no voy a adivinar", reply.lower())
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(PymeTicket.query.count(), 0)

    def test_application_ogg_with_parameters_uses_audio_transcription_contract(self):
        self._create_sender(child_scoped=True)
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "",
            "MessageSid": "SMapplicationogg001",
            "MediaMessageSid": "MMapplicationogg001",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/media/note.ogg",
            "MediaContentType0": "Application/Ogg; codecs=opus",
        }
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SMapplicationoggreply001")
        media_response = MagicMock(content=b"ogg-opus")
        media_response.raise_for_status.return_value = None
        attachment = SimpleNamespace(
            id=701,
            url="https://cdn.example.test/note.ogg",
            mime="application/ogg; codecs=opus",
            nombre_original="note.ogg",
            analisis=None,
        )

        with (
            patch.object(webhook_module, "Client", return_value=child_client),
            patch.object(webhook_module.requests, "get", return_value=media_response),
            patch.object(webhook_module, "create_attachment_with_thumbnail", return_value=attachment),
            patch.object(webhook_module, "clasificar_adjunto_whatsapp") as classifier,
            patch.object(webhook_module, "create_whatsapp_assisted_intake", return_value=None),
            patch.object(webhook_module, "responder_chatboc", return_value=self._bot_response()) as responder,
            patch(
                "services.audio_transcription_service.transcribe_audio_bytes",
                return_value="Hay un árbol caído frente a la escuela",
            ) as transcribe,
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        CHILD_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        transcribe.assert_called_once_with(
            b"ogg-opus",
            "Application/Ogg; codecs=opus",
            cache_url=payload["MediaUrl0"],
        )
        classifier.assert_not_called()
        responder.assert_called_once()
        self.assertEqual(
            responder.call_args.kwargs["pregunta"],
            "Hay un árbol caído frente a la escuela",
        )

    def test_unanalysed_video_sticker_and_vcard_get_actionable_reply_without_ticket(self):
        self._create_sender(child_scoped=True)
        child_client = MagicMock()
        child_client.messages.create.return_value = SimpleNamespace(sid="SMmediafallbackreply001")
        cases = (
            ("video/mp4", "clip.mp4", "video"),
            ("image/webp", "", "sticker"),
            ("text/vcard", "persona.vcf", "contacto"),
        )

        with (
            patch.object(webhook_module, "Client", return_value=child_client),
            patch.object(webhook_module.requests, "get") as media_get,
            patch.object(webhook_module, "create_attachment_with_thumbnail") as create_attachment,
            patch.object(webhook_module, "clasificar_adjunto_whatsapp") as classifier,
            patch.object(webhook_module, "create_whatsapp_assisted_intake") as assisted_intake,
            patch.object(webhook_module, "responder_chatboc") as responder,
        ):
            media_response = MagicMock(content=b"bounded-media")
            media_response.raise_for_status.return_value = None
            media_get.return_value = media_response
            create_attachment.side_effect = [
                SimpleNamespace(
                    id=800 + index,
                    url=f"https://cdn.example.test/media-{index}",
                    mime=mime_type,
                    nombre_original=body or f"media-{index}",
                    analisis=None,
                )
                for index, (mime_type, body, _label) in enumerate(cases)
            ]

            for index, (mime_type, body, _label) in enumerate(cases):
                payload = {
                    "AccountSid": CHILD_ACCOUNT_SID,
                    "MessagingServiceSid": MESSAGING_SERVICE_SID,
                    "To": f"whatsapp:{SENDER_NUMBER}",
                    "From": f"whatsapp:{RECIPIENT_NUMBER}",
                    "Body": body,
                    "MessageSid": f"SMspecialmedia{index:03d}",
                    "MediaMessageSid": f"MMspecialmedia{index:03d}",
                    "NumMedia": "1",
                    "MediaUrl0": f"https://api.twilio.com/media/{index}",
                    "MediaContentType0": mime_type,
                }
                response = self.client.post(
                    "/webhook/whatsapp",
                    data=payload,
                    headers={
                        "X-Twilio-Signature": self._signature(
                            "/webhook/whatsapp",
                            payload,
                            CHILD_AUTH_TOKEN,
                        )
                    },
                )
                self.assertEqual(response.status_code, 200)

        self.assertEqual(media_get.call_count, len(cases))
        self.assertEqual(create_attachment.call_count, len(cases))
        self.assertEqual(child_client.messages.create.call_count, len(cases))
        classifier.assert_not_called()
        assisted_intake.assert_not_called()
        responder.assert_not_called()
        rendered_replies = "\n".join(
            call.kwargs["body"].lower()
            for call in child_client.messages.create.call_args_list
        )
        for _mime_type, _body, label in cases:
            self.assertIn(label, rendered_replies)
        self.assertNotIn("persona.vcf", rendered_replies)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(PymeTicket.query.count(), 0)

    def test_parent_signature_is_rejected_for_child_sender(self):
        self._create_sender(child_scoped=True)
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "Necesito informacion",
        }

        with (
            patch.object(webhook_module, "Client") as client_factory,
            patch.object(webhook_module, "responder_chatboc") as responder,
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        PARENT_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 403)
        client_factory.assert_not_called()
        responder.assert_not_called()
        self.parent_client.messages.create.assert_not_called()

    def test_child_status_signature_is_accepted_and_parent_is_rejected(self):
        sender = self._create_sender(child_scoped=True)
        accepted_payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "MessageSid": "SM_child_status",
            "MessageStatus": "delivered",
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
        }
        accepted = self.client.post(
            "/twilio/whatsapp/status",
            data=accepted_payload,
            headers={
                "X-Twilio-Signature": self._signature(
                    "/twilio/whatsapp/status",
                    accepted_payload,
                    CHILD_AUTH_TOKEN,
                )
            },
        )

        rejected_payload = dict(accepted_payload, MessageSid="SM_parent_status")
        rejected = self.client.post(
            "/twilio/whatsapp/status",
            data=rejected_payload,
            headers={
                "X-Twilio-Signature": self._signature(
                    "/twilio/whatsapp/status",
                    rejected_payload,
                    PARENT_AUTH_TOKEN,
                )
            },
        )

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 403)
        event = MessagingEventLedger.query.filter_by(
            tenant_id=sender.tenant_id,
            provider_event_id="SM_child_status:delivered",
        ).one()
        self.assertEqual(event.provider_sender_id, sender.id)
        self.assertIsNone(
            MessagingEventLedger.query.filter_by(
                tenant_id=sender.tenant_id,
                provider_event_id="SM_parent_status:delivered",
            ).first()
        )

    def test_signed_child_status_reconciles_tenant_bound_notification_attempt(self):
        sender = self._create_sender(child_scoped=True)
        notification = Notification(
            tenant_id=sender.tenant_id,
            channel="whatsapp",
            recipient=RECIPIENT_NUMBER,
            body="Vista local",
            status=Notification.STATUS_SEND_UNCERTAIN,
            idempotency_key="notification-callback-scoped-1",
            max_retries=3,
            attempt_count=1,
            provider_sender_id=sender.id,
            provider_status=Notification.PROVIDER_STATUS_UNKNOWN,
            last_error="twilio_timeout",
        )
        db.session.add(notification)
        db.session.flush()
        attempt = NotificationAttempt(
            notification_id=notification.id,
            tenant_id=sender.tenant_id,
            attempt_number=1,
            status=NotificationAttempt.STATUS_SEND_UNCERTAIN,
            provider="whatsapp",
            provider_status=Notification.PROVIDER_STATUS_UNKNOWN,
            metadata_json={"provider_call_started": True},
        )
        db.session.add(attempt)
        db.session.commit()

        callback_path = (
            "/twilio/whatsapp/status?notification_attempt_id=" f"{attempt.id}"
        )
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "MessageSid": "SM_notification_callback_scoped",
            "MessageStatus": "delivered",
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
        }
        response = self.client.post(
            callback_path,
            data=payload,
            headers={
                "X-Twilio-Signature": self._signature(
                    callback_path,
                    payload,
                    CHILD_AUTH_TOKEN,
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        persisted = db.session.get(Notification, notification.id)
        persisted_attempt = db.session.get(NotificationAttempt, attempt.id)
        self.assertEqual(persisted.status, Notification.STATUS_SENT)
        self.assertEqual(persisted.provider_status, "delivered")
        self.assertEqual(
            persisted.provider_message_id,
            "SM_notification_callback_scoped",
        )
        self.assertEqual(persisted_attempt.status, NotificationAttempt.STATUS_SUCCESS)
        self.assertIsNotNone(persisted_attempt.delivery_event_id)

    def test_tenant_ticket_reply_callback_requires_signature_and_is_idempotent(self):
        sender = self._create_sender(child_scoped=True)
        ticket = TenantTicket(
            tenant_id=sender.tenant_id,
            categoria="luminarias",
            descripcion="Luminaria apagada",
            estado="en_proceso",
            origen="whatsapp",
        )
        db.session.add(ticket)
        db.session.flush()
        reply = TenantTicketReplyEvent(
            tenant_id=sender.tenant_id,
            ticket_id=ticket.id,
            event_id="reply-signed-callback-0001",
            body="Respuesta privada del operador",
            recipient_phone=RECIPIENT_NUMBER,
            whatsapp_delivery_status="uncertain",
        )
        db.session.add(reply)
        db.session.commit()

        callback_path = (
            "/twilio/whatsapp/status?tenant_ticket_reply_event_id=" f"{reply.id}"
        )
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "MessageSid": "SM_reply_callback_scoped",
            "MessageStatus": "delivered",
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
        }

        rejected = self.client.post(
            callback_path,
            data=payload,
            headers={"X-Twilio-Signature": "forged"},
        )
        self.assertEqual(rejected.status_code, 403)
        db.session.expire_all()
        self.assertEqual(
            db.session.get(TenantTicketReplyEvent, reply.id).whatsapp_delivery_status,
            "uncertain",
        )

        signature = self._signature(callback_path, payload, CHILD_AUTH_TOKEN)
        accepted = self.client.post(
            callback_path,
            data=payload,
            headers={"X-Twilio-Signature": signature},
        )
        replay = self.client.post(
            callback_path,
            data=payload,
            headers={"X-Twilio-Signature": signature},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        db.session.expire_all()
        persisted = db.session.get(TenantTicketReplyEvent, reply.id)
        self.assertEqual(persisted.whatsapp_delivery_status, "delivered")
        self.assertEqual(
            persisted.whatsapp_provider_message_id,
            "SM_reply_callback_scoped",
        )
        self.assertEqual(
            AuditEvent.query.filter_by(
                tenant_id=sender.tenant_id,
                event_type="tenant_ticket.reply.whatsapp.delivery_callback",
            ).count(),
            1,
        )

    def test_tenant_ticket_reply_callback_collision_is_quarantined_terminally(self):
        sender = self._create_sender(child_scoped=True)
        ticket = TenantTicket(
            tenant_id=sender.tenant_id,
            categoria="luminarias",
            descripcion="Reclamo con dos respuestas",
            estado="en_proceso",
            origen="whatsapp",
        )
        db.session.add(ticket)
        db.session.flush()
        provider_message_id = "SM_reply_callback_collision"
        owner = TenantTicketReplyEvent(
            tenant_id=sender.tenant_id,
            ticket_id=ticket.id,
            event_id="reply-callback-collision-owner",
            body="Respuesta original",
            recipient_phone=RECIPIENT_NUMBER,
            whatsapp_delivery_status="provider_accepted",
            whatsapp_provider_message_id=provider_message_id,
            whatsapp_provider_sender_id=sender.id,
            whatsapp_provider_status="accepted",
        )
        collided = TenantTicketReplyEvent(
            tenant_id=sender.tenant_id,
            ticket_id=ticket.id,
            event_id="reply-callback-collision-target",
            body="Respuesta que queda en cuarentena",
            recipient_phone=RECIPIENT_NUMBER,
            whatsapp_delivery_status="uncertain",
            whatsapp_provider_sender_id=sender.id,
        )
        db.session.add_all([owner, collided])
        db.session.commit()

        callback_path = (
            "/twilio/whatsapp/status?tenant_ticket_reply_event_id="
            f"{collided.id}"
        )
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "MessageSid": provider_message_id,
            "MessageStatus": "delivered",
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
        }
        response = self.client.post(
            callback_path,
            data=payload,
            headers={
                "X-Twilio-Signature": self._signature(
                    callback_path,
                    payload,
                    CHILD_AUTH_TOKEN,
                )
            },
        )

        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        persisted = db.session.get(TenantTicketReplyEvent, collided.id)
        self.assertEqual(persisted.whatsapp_delivery_status, "uncertain")
        self.assertIsNone(persisted.whatsapp_provider_message_id)
        self.assertEqual(
            persisted.whatsapp_error_code,
            "whatsapp_provider_message_id_duplicate",
        )
        audit = AuditEvent.query.filter_by(
            tenant_id=sender.tenant_id,
            event_type="tenant_ticket.reply.whatsapp.provider_message_collision",
            resource_id=str(collided.id),
        ).one()
        self.assertFalse(audit.details["automatic_retry_allowed"])
        self.assertNotIn(provider_message_id, str(audit.details))

    def test_legacy_sender_uses_parent_validator_and_parent_client(self):
        self._create_sender(child_scoped=False, create_provider_sender=False)
        payload = {
            "AccountSid": PARENT_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "Necesito informacion",
            "MessageSid": "SM_legacy_inbound",
        }

        with (
            patch.object(webhook_module, "Client") as client_factory,
            patch.object(webhook_module, "responder_chatboc", return_value=self._bot_response()),
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        PARENT_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        client_factory.assert_not_called()
        self.parent_client.messages.create.assert_called()

    def test_missing_child_secret_does_not_fall_back_to_parent(self):
        self._create_sender(child_scoped=True)
        self.app.config[CHILD_TOKEN_REF] = ""
        tenant_ref = "TWILIO_SUBACCOUNT_AUTH_TOKEN_SCOPED_TENANT_1"
        self.app.config[tenant_ref] = ""
        self.app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN"] = "unrelated-child-secret"
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "Body": "Necesito informacion",
        }
        empty_secrets = {
            CHILD_TOKEN_REF: "",
            tenant_ref: "",
            "TWILIO_SUBACCOUNT_AUTH_TOKEN": "unrelated-child-secret",
        }

        with (
            patch.dict(os.environ, empty_secrets, clear=False),
            patch.object(webhook_module, "Client") as client_factory,
        ):
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": self._signature(
                        "/webhook/whatsapp",
                        payload,
                        PARENT_AUTH_TOKEN,
                    )
                },
            )

        self.assertEqual(response.status_code, 503)
        client_factory.assert_not_called()
        self.parent_client.messages.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
