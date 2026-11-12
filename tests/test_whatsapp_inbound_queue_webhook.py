import hashlib
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
    ChannelSessionIdentityBinding,
    ChatSessionContext,
    MunicipioTicket,
    ProviderSender,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    User,
    WhatsappNumero,
    WhatsAppInboundTurn,
    WhatsAppOutboundAttempt,
)
from routes import whatsapp_webhook as webhook_module
from services import whatsapp_inbound_worker as worker_module
from services.whatsapp_inbound_durability import (
    WhatsAppInboundDurabilityConfigurationError,
)
from services.whatsapp_inbound_turns import claim_next_whatsapp_inbound_turn


PARENT_ACCOUNT_SID = "ACqueue_parent"
PARENT_AUTH_TOKEN = "queue-parent-secret"
CHILD_ACCOUNT_SID = "ACqueue_child"
CHILD_AUTH_TOKEN = "queue-child-secret"
CHILD_TOKEN_REF = "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACQUEUE_CHILD"
MESSAGING_SERVICE_SID = "MG_queue_sender"
SENDER_NUMBER = "+15550103030"
RECIPIENT_NUMBER = "+15550909090"
SECOND_CHILD_ACCOUNT_SID = "ACqueue_child_second"
SECOND_CHILD_AUTH_TOKEN = "queue-child-second-secret"
SECOND_CHILD_TOKEN_REF = "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACQUEUE_CHILD_SECOND"
SECOND_MESSAGING_SERVICE_SID = "MG_queue_sender_second"
SECOND_SENDER_NUMBER = "+15550104040"
STREAM_SECRET = "queue-stream-secret-with-at-least-32-bytes"


class QueueWebhookConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False, "timeout": 5}
    }
    WTF_CSRF_ENABLED = False
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    TWILIO_ACCOUNT_SID = PARENT_ACCOUNT_SID
    TWILIO_AUTH_TOKEN = PARENT_AUTH_TOKEN
    TWILIO_ALLOW_NETWORK_IN_TESTS = True
    PUBLIC_API_BASE_URL = "http://localhost"
    BACKEND_URL = "http://localhost"
    WHATSAPP_AUDIO_ENABLED = False
    WHATSAPP_INBOUND_DURABILITY_MODE = "queue"
    WHATSAPP_INBOUND_HASH_SECRET = STREAM_SECRET
    CHANNEL_SESSION_IDENTITY_MODE = "enforce"
    CHANNEL_SESSION_IDENTITY_HMAC_SECRET_V1 = (
        "queue-session-identity-secret-with-at-least-32-bytes"
    )
    CHANNEL_SESSION_IDENTITY_VERSION_V1 = "v1"
    WHATSAPP_INBOUND_LEASE_SECONDS = 180
    WHATSAPP_INBOUND_MAX_ATTEMPTS = 4
    WHATSAPP_INBOUND_WORKER_BATCH_SIZE = 4


class WhatsAppInboundQueueWebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app(QueueWebhookConfig)
        self.app.config[CHILD_TOKEN_REF] = CHILD_AUTH_TOKEN
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        owner = User(
            name="Queue Tenant",
            email="queue-tenant@example.com",
            rol="empresa",
            tipo_chat="pyme",
            nombre_empresa="Queue Tenant",
        )
        owner.set_password("test-password")
        db.session.add(owner)
        db.session.flush()
        self.owner = owner

        self.tenant = TenantProfile(
            slug="queue-tenant",
            nombre="Queue Tenant",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={
                "twilio_tech_provider": {
                    "twilio_account_sid": CHILD_ACCOUNT_SID,
                    "twilio_subaccount_token_ref": CHILD_TOKEN_REF,
                    "messaging_service_sid": MESSAGING_SERVICE_SID,
                }
            },
        )
        db.session.add(self.tenant)
        db.session.flush()

        self.sender = ProviderSender(
            tenant_id=self.tenant.id,
            channel="whatsapp",
            phone_number=SENDER_NUMBER,
            sender_id=f"whatsapp:{SENDER_NUMBER}",
            messaging_service_sid=MESSAGING_SERVICE_SID,
            status="active",
        )
        db.session.add(self.sender)
        db.session.add(
            WhatsappNumero(
                numero_whatsapp=SENDER_NUMBER,
                user_id=owner.id,
                is_active=True,
            )
        )
        db.session.commit()
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(self.tenant.id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @staticmethod
    def _signature(payload: dict) -> str:
        return RequestValidator(CHILD_AUTH_TOKEN).compute_signature(
            "http://localhost/webhook/whatsapp",
            payload,
        )

    @staticmethod
    def _payload(sid: str, *, body: str = "Necesito informacion") -> dict:
        return {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "WaId": RECIPIENT_NUMBER.removeprefix("+"),
            "Body": body,
            "MessageSid": sid,
            "SmsMessageSid": sid,
            "NumMedia": "0",
        }

    def _post(self, payload: dict, *, signature: str | None = None):
        return self.client.post(
            "/webhook/whatsapp",
            data=payload,
            headers={
                "X-Twilio-Signature": signature or self._signature(payload),
            },
        )

    def _create_secondary_sender_scope(self):
        owner = User(
            name="Excluded Queue Tenant",
            email="excluded-queue-tenant@example.com",
            rol="empresa",
            tipo_chat="pyme",
            nombre_empresa="Excluded Queue Tenant",
        )
        owner.set_password("test-password")
        db.session.add(owner)
        db.session.flush()
        tenant = TenantProfile(
            slug="excluded-queue-tenant",
            nombre="Excluded Queue Tenant",
            tipo="pyme",
            pyme_id=owner.id,
            configuracion={
                "twilio_tech_provider": {
                    "twilio_account_sid": SECOND_CHILD_ACCOUNT_SID,
                    "twilio_subaccount_token_ref": SECOND_CHILD_TOKEN_REF,
                    "messaging_service_sid": SECOND_MESSAGING_SERVICE_SID,
                }
            },
        )
        db.session.add(tenant)
        db.session.flush()
        sender = ProviderSender(
            tenant_id=tenant.id,
            channel="whatsapp",
            phone_number=SECOND_SENDER_NUMBER,
            sender_id=f"whatsapp:{SECOND_SENDER_NUMBER}",
            messaging_service_sid=SECOND_MESSAGING_SERVICE_SID,
            status="active",
        )
        db.session.add(sender)
        db.session.add(
            WhatsappNumero(
                numero_whatsapp=SECOND_SENDER_NUMBER,
                user_id=owner.id,
                is_active=True,
            )
        )
        db.session.commit()
        self.app.config[SECOND_CHILD_TOKEN_REF] = SECOND_CHILD_AUTH_TOKEN
        return tenant, sender

    @staticmethod
    def _secondary_payload(sid: str) -> dict:
        return {
            "AccountSid": SECOND_CHILD_ACCOUNT_SID,
            "MessagingServiceSid": SECOND_MESSAGING_SERVICE_SID,
            "To": f"whatsapp:{SECOND_SENDER_NUMBER}",
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "WaId": RECIPIENT_NUMBER.removeprefix("+"),
            "Body": "Mensaje para tenant fuera del canario",
            "MessageSid": sid,
            "SmsMessageSid": sid,
            "NumMedia": "0",
        }

    @staticmethod
    def _secondary_signature(payload: dict) -> str:
        return RequestValidator(SECOND_CHILD_AUTH_TOKEN).compute_signature(
            "http://localhost/webhook/whatsapp",
            payload,
        )

    def test_invalid_signature_never_persists_turn(self):
        payload = self._payload("SMinvalidsignature001")

        response = self._post(payload, signature="invalid-signature")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)

    def test_misdirected_status_callback_is_tenant_bound_and_never_creates_a_turn(self):
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
            "MessageSid": "SMmisdirectedstatus001",
            "MessageStatus": "delivered",
        }

        with (
            patch.object(webhook_module, "Client") as client_factory,
            patch.object(webhook_module, "responder_chatboc") as responder,
            patch.object(worker_module, "enqueue_whatsapp_inbound_stream") as enqueue,
        ):
            response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        self.assertEqual(ChatSessionContext.query.count(), 0)
        self.assertEqual(User.query.count(), 1)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(PymeTicket.query.count(), 0)
        client_factory.assert_not_called()
        responder.assert_not_called()
        enqueue.assert_not_called()

    def test_call_control_event_without_message_sid_never_enters_conversation(self):
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "From": f"whatsapp:{RECIPIENT_NUMBER}",
            "To": f"whatsapp:{SENDER_NUMBER}",
            "CallSid": "CAwhatsappcontrol001",
            "CallStatus": "ringing",
        }

        with (
            patch.object(webhook_module, "Client") as client_factory,
            patch.object(webhook_module, "responder_chatboc") as responder,
            patch.object(worker_module, "enqueue_whatsapp_inbound_stream") as enqueue,
        ):
            response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        self.assertEqual(ChatSessionContext.query.count(), 0)
        self.assertEqual(User.query.count(), 1)
        client_factory.assert_not_called()
        responder.assert_not_called()
        enqueue.assert_not_called()

    def test_cross_tenant_signature_cannot_ack_control_event(self):
        payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
            "MessageSid": "SMcrossscopestatus001",
            "MessageStatus": "read",
        }
        wrong_signature = RequestValidator(PARENT_AUTH_TOKEN).compute_signature(
            "http://localhost/webhook/whatsapp",
            payload,
        )

        response = self._post(payload, signature=wrong_signature)

        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        self.assertEqual(ChatSessionContext.query.count(), 0)
        self.assertEqual(User.query.count(), 1)

    def test_excluded_tenant_stays_legacy_and_form_fields_cannot_enable_queue(self):
        excluded_tenant, _ = self._create_secondary_sender_scope()
        payload = self._secondary_payload("SMexcludedtenantlegacy001")
        payload.update(
            {
                "TenantId": str(self.tenant.id),
                "tenant_id": str(self.tenant.id),
                "ProviderSenderId": str(self.sender.id),
            }
        )
        provider_client = MagicMock()
        provider_client.messages.create.return_value = SimpleNamespace(
            sid="SMexcludedlegacyreply001",
            status="queued",
        )

        with (
            patch.object(webhook_module, "Client", return_value=provider_client),
            patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta legacy del tenant excluido.",
                    "message_type": "text",
                    "options_list": [],
                },
            ) as responder,
        ):
            response = self._post(
                payload,
                signature=self._secondary_signature(payload),
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(excluded_tenant.id, {self.tenant.id})
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        responder.assert_called_once()
        provider_client.messages.create.assert_called()

    def test_allowed_signature_cannot_cross_route_an_excluded_sender(self):
        self._create_secondary_sender_scope()
        payload = self._secondary_payload("SMcrosssendercanary001")

        with patch.object(webhook_module, "responder_chatboc") as responder:
            response = self._post(payload, signature=self._signature(payload))

        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        responder.assert_not_called()

    def test_invalid_queue_allowlist_fails_closed_after_signature_and_sender_scope(self):
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = ""
        payload = self._payload("SMinvalidcanaryconfig001")

        with patch.object(webhook_module, "responder_chatboc") as responder:
            response = self._post(payload)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
        self.assertEqual(ChannelSessionIdentityBinding.query.count(), 0)
        responder.assert_not_called()

    def test_worker_tasks_and_broker_never_claim_an_excluded_tenant(self):
        excluded_tenant_id = self.tenant.id
        canary_tenant_id = excluded_tenant_id + 1000
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(canary_tenant_id)

        with (
            patch.object(worker_module, "claim_next_whatsapp_inbound_turn") as inbound_claim,
            patch.object(worker_module, "claim_next_whatsapp_outbound_attempt") as outbound_claim,
            patch.object(
                worker_module.process_whatsapp_inbound_stream_task,
                "apply_async",
            ) as apply_async,
        ):
            inbound_claim.return_value = None
            outbound_claim.return_value = None
            with self.assertRaisesRegex(
                WhatsAppInboundDurabilityConfigurationError,
                "whatsapp_inbound_worker_tenant_not_canary",
            ):
                worker_module.process_whatsapp_inbound_stream(
                    tenant_id=excluded_tenant_id,
                    stream_key="excluded-stream",
                    limit=1,
                )
            with self.assertRaisesRegex(
                WhatsAppInboundDurabilityConfigurationError,
                "whatsapp_inbound_worker_tenant_not_canary",
            ):
                worker_module.process_whatsapp_inbound_stream_task.run(
                    excluded_tenant_id,
                    "excluded-stream",
                )
            with self.assertRaisesRegex(
                WhatsAppInboundDurabilityConfigurationError,
                "whatsapp_inbound_worker_tenant_not_canary",
            ):
                worker_module.dispatch_next_whatsapp_outbound_attempt(
                    tenant_id=excluded_tenant_id
                )
            self.assertFalse(
                worker_module.enqueue_whatsapp_inbound_stream(
                    tenant_id=excluded_tenant_id,
                    stream_key="excluded-stream",
                )
            )

            sweep = worker_module.sweep_whatsapp_inbound_turns_task.run(limit=1)
            outbound = worker_module.dispatch_whatsapp_outbound_attempts(limit=1)

        self.assertEqual(sweep["processed"], 0)
        self.assertEqual(outbound["processed"], 0)
        self.assertEqual(inbound_claim.call_args.kwargs["tenant_id"], canary_tenant_id)
        self.assertEqual(outbound_claim.call_args.kwargs["tenant_id"], canary_tenant_id)
        apply_async.assert_not_called()

    def test_sweep_leaves_excluded_tenant_backlog_unclaimed_and_recoverable(self):
        payload = self._payload("SMexcludedbacklog001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(
            self.tenant.id + 1000
        )

        result = worker_module.process_whatsapp_inbound_stream(limit=1)

        self.assertEqual(result["processed"], 0)
        db.session.expire_all()
        retained = WhatsAppInboundTurn.query.one()
        self.assertEqual(retained.status, WhatsAppInboundTurn.STATUS_RECEIVED)
        self.assertIsNone(retained.lease_token)
        self.assertEqual(retained.attempt_count, 0)

    def test_outbound_dispatch_leaves_removed_canary_attempt_unclaimed(self):
        payload = self._payload("SMexcludedoutboundbacklog001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta durable pendiente.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )
        self.assertEqual(processed["completed"], 1, processed)
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(
            self.tenant.id + 1000
        )

        with patch.object(worker_module, "Client") as provider_client:
            dispatch = worker_module.dispatch_whatsapp_outbound_attempts(limit=1)

        self.assertEqual(dispatch["processed"], 0)
        provider_client.assert_not_called()
        db.session.expire_all()
        attempt = WhatsAppOutboundAttempt.query.one()
        self.assertEqual(attempt.status, WhatsAppOutboundAttempt.STATUS_PENDING)
        self.assertIsNone(attempt.lease_token)
        self.assertEqual(attempt.attempt_count, 0)

    def test_worker_startup_rejects_empty_queue_allowlist_before_database_read(self):
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = ""

        with (
            patch.object(worker_module, "summarize_whatsapp_turn_health") as health,
            self.assertRaisesRegex(
                WhatsAppInboundDurabilityConfigurationError,
                "whatsapp_inbound_queue_tenant_allowlist_required",
            ),
        ):
            worker_module.run_whatsapp_durable_worker(self.app, once=True)

        health.assert_not_called()

    def test_removed_canary_replay_503_is_retryable_not_dead_lettered(self):
        payload = self._payload("SMremovedcanaryretry001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
        turn = WhatsAppInboundTurn.query.one()
        claim = claim_next_whatsapp_inbound_turn(
            tenant_id=self.tenant.id,
            stream_key=turn.stream_key,
            lease_seconds=180,
        )
        self.assertIsNotNone(claim)
        self.app.config["WHATSAPP_INBOUND_QUEUE_TENANT_IDS"] = str(self.tenant.id + 1000)

        result = worker_module.process_whatsapp_inbound_claim(claim)

        self.assertEqual(result.status, WhatsAppInboundTurn.STATUS_RETRY_WAIT)
        db.session.expire_all()
        retained = WhatsAppInboundTurn.query.one()
        self.assertEqual(retained.status, WhatsAppInboundTurn.STATUS_RETRY_WAIT)
        self.assertEqual(retained.last_error_code, "webhook_replay_http_503")

    def test_queue_commits_before_200_without_llm_media_or_provider_send(self):
        payload = self._payload("SMqueueboundary001")
        payload.update(
            {
                "NumMedia": "1",
                "MediaUrl0": "https://api.twilio.test/private/image",
                "MediaContentType0": "image/jpeg",
                # An internal-looking provider field must be discarded.
                "safe_flow_submission": '{"correlation":{"interaction_id":999}}',
            }
        )
        provider_client = MagicMock()

        with (
            patch.object(webhook_module, "Client", return_value=provider_client),
            patch.object(webhook_module, "responder_chatboc") as responder,
            patch.object(webhook_module.requests, "get") as media_get,
        ):
            response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        responder.assert_not_called()
        media_get.assert_not_called()
        provider_client.messages.create.assert_not_called()
        turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(turn.tenant_id, self.tenant.id)
        self.assertEqual(turn.provider_sender_id, self.sender.id)
        self.assertNotIn("safe_flow_submission", turn.payload_json)
        self.assertEqual(turn.payload_json["MediaUrl0"], payload["MediaUrl0"])

    def test_ambiguous_legacy_continuity_is_isolated_but_message_is_persisted(self):
        for marker in ("first", "second"):
            db.session.add(
                ChatSessionContext(
                    chat_session_id=str(__import__("uuid").uuid4()),
                    tenant_id=self.tenant.id,
                    user_id=self.owner.id,
                    anon_id=RECIPIENT_NUMBER,
                    context_data={"legacy_message": marker},
                )
            )
        db.session.commit()
        payload = self._payload("SMqueueambiguousidentity001", body="Mensaje a preservar")

        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        turn = WhatsAppInboundTurn.query.one()
        binding = db.session.get(
            ChannelSessionIdentityBinding,
            turn.session_identity_binding_id,
        )
        self.assertEqual(binding.continuity_status, "isolated")
        self.assertEqual(binding.last_conflict_code, "legacy_membership_ambiguous")
        self.assertEqual(turn.payload_json["Body"], "Mensaje a preservar")
        self.assertEqual(turn.chat_session_id, binding.chat_session_id)
        self.assertNotIn(RECIPIENT_NUMBER, turn.chat_session_id)
        self.assertEqual(
            {
                row.context_data.get("legacy_message")
                for row in ChatSessionContext.query.filter_by(
                    tenant_id=self.tenant.id,
                    anon_id=RECIPIENT_NUMBER,
                ).all()
            },
            {"first", "second"},
        )

    def test_sync_webhook_uses_same_canonical_binding_without_phone_session_id(self):
        self.app.config["WHATSAPP_INBOUND_DURABILITY_MODE"] = "legacy"
        payload = self._payload("SMsyncidentity001")
        provider_client = MagicMock()
        provider_client.messages.create.return_value = SimpleNamespace(
            sid="SMsyncidentityreply001",
            status="queued",
        )

        with (
            patch.object(webhook_module, "Client", return_value=provider_client),
            patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta canonica.",
                    "message_type": "text",
                    "options_list": [],
                },
            ),
        ):
            response = self._post(payload)

        self.assertEqual(response.status_code, 200)
        binding = ChannelSessionIdentityBinding.query.filter_by(
            tenant_id=self.tenant.id,
            channel="whatsapp",
        ).one()
        session = db.session.get(ChatSessionContext, binding.chat_session_id)
        self.assertIsNotNone(session)
        self.assertNotIn(RECIPIENT_NUMBER, session.chat_session_id)
        self.assertEqual(session.context_data["telefono_usuario"], RECIPIENT_NUMBER)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 0)

    def test_worker_replay_rejects_quarantined_binding_and_retains_payload(self):
        payload = self._payload("SMqueuereplayidentity001", body="No perder este mensaje")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
        turn = WhatsAppInboundTurn.query.one()
        binding = db.session.get(
            ChannelSessionIdentityBinding,
            turn.session_identity_binding_id,
        )
        binding.status = ChannelSessionIdentityBinding.STATUS_QUARANTINED
        binding.last_conflict_code = "operator_quarantine_test"
        db.session.commit()

        result = worker_module.process_whatsapp_inbound_stream(
            tenant_id=self.tenant.id,
            stream_key=turn.stream_key,
            limit=1,
        )

        self.assertEqual(result["processed"], 1, result)
        self.assertEqual(result["completed"], 0, result)
        db.session.expire_all()
        retained = WhatsAppInboundTurn.query.one()
        self.assertEqual(retained.status, WhatsAppInboundTurn.STATUS_DEAD)
        self.assertEqual(retained.payload_json["Body"], "No perder este mensaje")
        self.assertEqual(retained.last_error_code, "webhook_replay_http_403")

    def test_duplicate_same_sid_is_one_row_and_changed_digest_conflicts(self):
        payload = self._payload("SMqueueduplicate001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            first = self._post(payload)
            duplicate = self._post(payload)

            changed = dict(payload, Body="Contenido alterado")
            conflict = self._post(changed)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(WhatsAppInboundTurn.query.count(), 1)

    def test_signed_twilio_retry_canary_persists_and_processes_same_sid_once(self):
        payload = self._payload(
            "SMqueuesignedretrycanary001",
            body="Canario local de retry durable",
        )
        signature = self._signature(payload)
        validator = RequestValidator(CHILD_AUTH_TOKEN)
        self.assertTrue(
            validator.validate(
                "http://localhost/webhook/whatsapp",
                payload,
                signature,
            )
        )

        provider_client = MagicMock()
        bot_payload = {
            "message_body": "Respuesta unica del canario durable.",
            "message_type": "text",
            "options_list": [],
        }

        def post_retry(idempotency_token: str):
            return self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={
                    "X-Twilio-Signature": signature,
                    "I-Twilio-Idempotency-Token": idempotency_token,
                },
            )

        with (
            patch.object(
                webhook_module,
                "Client",
                return_value=provider_client,
            ),
            patch.object(worker_module, "enqueue_whatsapp_inbound_stream") as enqueue,
            patch.object(
                webhook_module,
                "responder_chatboc",
                return_value=bot_payload,
            ) as responder,
        ):
            self.app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
            fenced = post_retry("retry-token-canary-fenced")

            self.assertEqual(fenced.status_code, 503)
            self.assertEqual(
                fenced.get_json(),
                {
                    "contract_version": "cutover.writer_fence.v1",
                    "status": "maintenance",
                    "reason_code": "cutover_writer_fence_enabled",
                    "retryable": True,
                },
            )
            self.assertEqual(fenced.headers.get("Cache-Control"), "no-store")
            self.assertEqual(fenced.headers.get("Retry-After"), "60")
            self.assertEqual(WhatsAppInboundTurn.query.count(), 0)
            enqueue.assert_not_called()
            responder.assert_not_called()

            self.app.config["CUTOVER_WRITER_FENCE_ENABLED"] = False
            first = post_retry("retry-token-canary-accepted")
            retry = post_retry("retry-token-canary-duplicate")

            for response in (first, retry):
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), "OK")
            responder.assert_not_called()

            turns = WhatsAppInboundTurn.query.all()
            self.assertEqual(len(turns), 1)
            turn = turns[0]
            self.assertEqual(turn.provider_message_sid, payload["MessageSid"])
            self.assertNotIn("I-Twilio-Idempotency-Token", turn.payload_json)
            self.assertNotIn("retry-token-canary-fenced", str(turn.payload_json))
            self.assertNotIn("retry-token-canary-accepted", str(turn.payload_json))
            self.assertNotIn("retry-token-canary-duplicate", str(turn.payload_json))
            enqueue.assert_called_once_with(
                tenant_id=self.tenant.id,
                stream_key=turn.stream_key,
            )

            first_worker_run = worker_module.process_whatsapp_inbound_stream(
                tenant_id=self.tenant.id,
                stream_key=turn.stream_key,
                limit=1,
            )
            second_worker_run = worker_module.process_whatsapp_inbound_stream(
                tenant_id=self.tenant.id,
                stream_key=turn.stream_key,
                limit=1,
            )

        self.assertEqual(first_worker_run["processed"], 1, first_worker_run)
        self.assertEqual(first_worker_run["completed"], 1, first_worker_run)
        self.assertEqual(second_worker_run["processed"], 0, second_worker_run)
        self.assertEqual(second_worker_run["completed"], 0, second_worker_run)
        responder.assert_called_once()
        provider_client.messages.create.assert_not_called()

        db.session.expire_all()
        completed_turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(
            completed_turn.status,
            WhatsAppInboundTurn.STATUS_COMPLETED,
        )
        matching_attempts = [
            attempt
            for attempt in WhatsAppOutboundAttempt.query.all()
            if bot_payload["message_body"]
            in str(attempt.payload_json.get("body") or "")
        ]
        self.assertEqual(len(matching_attempts), 1)

    def test_worker_replays_scoped_turn_and_stages_outbox_without_sending(self):
        payload = self._payload("SMqueueworker001")
        provider_client = MagicMock()
        provider_client.messages.create.return_value = SimpleNamespace(
            sid="SM_should_not_send_during_replay",
            status="queued",
        )
        bot_payload = {
            "message_body": "Respuesta durable del tenant.",
            "message_type": "text",
            "options_list": [],
        }

        with patch.object(webhook_module, "Client", return_value=provider_client) as factory:
            inbound = self._post(payload)
            self.assertEqual(inbound.status_code, 200)
            turn = WhatsAppInboundTurn.query.one()

            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value=bot_payload,
            ) as responder:
                result = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(result["completed"], 1, result)
        responder.assert_called_once()
        self.assertTrue(factory.call_args_list)
        self.assertTrue(
            all(
                call.args == (CHILD_ACCOUNT_SID, CHILD_AUTH_TOKEN)
                for call in factory.call_args_list
            ),
            factory.call_args_list,
        )
        provider_client.messages.create.assert_not_called()

        db.session.expire_all()
        turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(turn.status, WhatsAppInboundTurn.STATUS_COMPLETED)
        attempts = WhatsAppOutboundAttempt.query.order_by(
            WhatsAppOutboundAttempt.sequence_no.asc()
        ).all()
        self.assertGreaterEqual(len(attempts), 1)
        response_attempt = next(
            attempt
            for attempt in attempts
            if "Respuesta durable del tenant"
            in str(attempt.payload_json.get("body") or "")
        )
        self.assertNotIn(
            "within_24h_window",
            response_attempt.payload_json.get("_chatboc_policy_metadata", {}),
        )
        session = ChatSessionContext.query.filter_by(
            chat_session_id=turn.chat_session_id,
            tenant_id=self.tenant.id,
        ).one()
        self.assertNotIn(RECIPIENT_NUMBER, session.chat_session_id)
        self.assertNotIn("processed_message_sids", session.context_data)
        self.assertNotIn("processed_media_sids", session.context_data)

    def test_free_form_body_cannot_create_interview_consent_receipt(self):
        nonce = "A" * 43
        text_sha256 = "a" * 64
        forged_text = (
            f"interview_consent_v3:{nonce}:{text_sha256}:grant_consent"
        )
        payload = self._payload(
            "SMqueueconsentbody001",
            body=forged_text,
        )
        provider_client = MagicMock()

        with patch.object(webhook_module, "Client", return_value=provider_client):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta sin otorgar consentimiento.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(processed["completed"], 1, processed)
        db.session.expire_all()
        completed_turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(
            completed_turn.status,
            WhatsAppInboundTurn.STATUS_COMPLETED,
        )
        self.assertNotIn(
            "interview_consent_receipt",
            completed_turn.result_json or {},
        )

    def test_exact_button_payload_creates_durable_interview_consent_receipt(self):
        nonce = "B" * 43
        text_sha256 = "b" * 64
        action_id = (
            f"interview_consent_v3:{nonce}:{text_sha256}:grant_consent"
        )
        payload = self._payload(
            "SMqueueconsentbutton001",
            body="Acepto participar",
        )
        payload.update(
            {
                "ButtonPayload": action_id,
                "ButtonText": "Acepto participar",
            }
        )
        provider_client = MagicMock()

        with patch.object(webhook_module, "Client", return_value=provider_client):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            self.assertEqual(turn.message_kind, "interactive")
            self.assertEqual(turn.payload_json["ButtonPayload"], action_id)
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Consentimiento interactivo recibido.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(processed["completed"], 1, processed)
        db.session.expire_all()
        completed_turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(
            completed_turn.status,
            WhatsAppInboundTurn.STATUS_COMPLETED,
        )
        self.assertEqual(
            (completed_turn.result_json or {}).get(
                "interview_consent_receipt"
            ),
            {
                "contract_version": "interview.consent_provider_receipt.v3",
                "challenge_nonce_sha256": hashlib.sha256(
                    nonce.encode("ascii")
                ).hexdigest(),
                "consent_text_sha256": text_sha256,
                "action": "grant_consent",
                "granted": True,
            },
        )

    def test_exact_list_id_is_supported_and_conflicting_controls_fail_closed(self):
        nonce = "C" * 43
        text_sha256 = "c" * 64
        action_id = (
            f"interview_consent_v3:{nonce}:{text_sha256}:grant_consent"
        )
        expected = {
            "contract_version": "interview.consent_provider_receipt.v3",
            "challenge_nonce_sha256": hashlib.sha256(
                nonce.encode("ascii")
            ).hexdigest(),
            "consent_text_sha256": text_sha256,
            "action": "grant_consent",
            "granted": True,
        }

        self.assertEqual(
            worker_module._extract_interview_consent_provider_receipt(
                {"ListId": action_id, "Body": "Texto visible no autoritativo"}
            ),
            expected,
        )
        self.assertIsNone(
            worker_module._extract_interview_consent_provider_receipt(
                {
                    "ButtonPayload": action_id,
                    "ListId": (
                        f"interview_consent_v3:{'D' * 43}:"
                        f"{text_sha256}:grant_consent"
                    ),
                }
            )
        )
        self.assertIsNone(
            worker_module._extract_interview_consent_provider_receipt(
                {"ButtonPayload": f" {action_id}"}
            )
        )

    def test_failed_media_replay_stages_one_honest_outbox_reply_without_ticket(self):
        payload = self._payload("SMqueuedmediafailure001", body="")
        payload.update(
            {
                "NumMedia": "1",
                "MediaUrl0": "https://api.twilio.test/private/unavailable.ogg",
                "MediaContentType0": "application/ogg; codecs=opus",
            }
        )
        provider_client = MagicMock()

        with patch.object(webhook_module, "Client", return_value=provider_client):
            inbound = self._post(payload)
            self.assertEqual(inbound.status_code, 200)
            turn = WhatsAppInboundTurn.query.one()

            with (
                patch.object(
                    webhook_module.requests,
                    "get",
                    side_effect=webhook_module.requests.exceptions.RequestException(
                        "provider unavailable"
                    ),
                ) as media_get,
                patch.object(webhook_module, "create_attachment_with_thumbnail") as create_attachment,
                patch.object(webhook_module, "create_whatsapp_assisted_intake") as assisted_intake,
                patch.object(webhook_module, "responder_chatboc") as responder,
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )
                replay = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(processed["completed"], 1, processed)
        self.assertEqual(replay["completed"], 0, replay)
        media_get.assert_called_once()
        create_attachment.assert_not_called()
        assisted_intake.assert_not_called()
        responder.assert_not_called()
        provider_client.messages.create.assert_not_called()
        attempts = WhatsAppOutboundAttempt.query.all()
        self.assertEqual(len(attempts), 1)
        reply = str((attempts[0].payload_json or {}).get("body") or "").lower()
        self.assertIn("no pude transcribirlo", reply)
        self.assertIn("no voy a adivinar", reply)
        self.assertEqual(TenantTicket.query.count(), 0)
        self.assertEqual(MunicipioTicket.query.count(), 0)
        self.assertEqual(PymeTicket.query.count(), 0)

    def test_provider_timeout_is_uncertain_and_never_claimed_again(self):
        payload = self._payload("SMqueuetimeout001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta para timeout.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )
        self.assertEqual(processed["completed"], 1, processed)

        timeout_client = MagicMock()
        timeout_client.messages.create.side_effect = TimeoutError("provider timeout")
        with patch.object(worker_module, "Client", return_value=timeout_client):
            first = worker_module.dispatch_next_whatsapp_outbound_attempt(
                tenant_id=self.tenant.id
            )
            second = worker_module.dispatch_next_whatsapp_outbound_attempt(
                tenant_id=self.tenant.id
            )

        self.assertEqual(first.status, "send_uncertain")
        self.assertEqual(second.status, "idle")
        self.assertEqual(timeout_client.messages.create.call_count, 1)
        provider_kwargs = timeout_client.messages.create.call_args.kwargs
        self.assertNotIn("_chatboc_policy_metadata", provider_kwargs)
        self.assertIn(
            f"outbound_attempt_id={first.attempt_id}",
            provider_kwargs["status_callback"],
        )
        self.assertTrue(
            provider_kwargs["status_callback"].endswith(
                "#rc=2&rp=5xx,ct,rt"
            ),
            provider_kwargs["status_callback"],
        )
        db.session.expire_all()
        attempt = WhatsAppOutboundAttempt.query.filter_by(
            attempt_id=first.attempt_id
        ).one()
        self.assertEqual(
            attempt.status,
            WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN,
        )

    def test_pre_provider_configuration_failure_is_retryable_not_uncertain(self):
        payload = self._payload("SMqueuepresend001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta pendiente de configuracion.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )
        self.assertEqual(processed["completed"], 1, processed)

        for key in (
            "TWILIO_WHATSAPP_STATUS_CALLBACK_URL",
            "WHATSAPP_STATUS_CALLBACK_URL",
            "PUBLIC_API_BASE_URL",
            "BACKEND_URL",
            "API_BASE_URL",
            "APP_BASE_URL",
            "BASE_URL",
            "RENDER_EXTERNAL_URL",
        ):
            self.app.config[key] = None
        provider_factory = MagicMock()
        with patch.object(worker_module, "Client", provider_factory):
            dispatch = worker_module.dispatch_next_whatsapp_outbound_attempt(
                tenant_id=self.tenant.id
            )

        self.assertEqual(dispatch.status, WhatsAppOutboundAttempt.STATUS_RETRY_WAIT)
        provider_factory.assert_not_called()
        db.session.expire_all()
        attempt = WhatsAppOutboundAttempt.query.filter_by(
            attempt_id=dispatch.attempt_id
        ).one()
        self.assertEqual(attempt.status, WhatsAppOutboundAttempt.STATUS_RETRY_WAIT)
        self.assertIsNone(attempt.provider_message_sid)

    def test_responder_failure_retries_durable_turn_instead_of_completing(self):
        payload = self._payload("SMqueueresponderfailure001")
        provider_client = MagicMock()
        with patch.object(webhook_module, "Client", return_value=provider_client):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                side_effect=RuntimeError("llm unavailable"),
            ):
                result = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(result["completed"], 0, result)
        self.assertEqual(
            result["results"][0]["status"],
            WhatsAppInboundTurn.STATUS_RETRY_WAIT,
        )
        db.session.expire_all()
        turn = WhatsAppInboundTurn.query.one()
        self.assertEqual(turn.status, WhatsAppInboundTurn.STATUS_RETRY_WAIT)
        self.assertEqual(WhatsAppOutboundAttempt.query.count(), 0)
        provider_client.messages.create.assert_not_called()

    def test_outbound_capture_failure_retries_durable_turn(self):
        payload = self._payload("SMqueuecapturefailure001")
        provider_client = MagicMock()
        with patch.object(webhook_module, "Client", return_value=provider_client):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with (
                patch.object(
                    webhook_module,
                    "responder_chatboc",
                    return_value={
                        "message_body": "Respuesta que no pudo persistirse.",
                        "message_type": "text",
                        "options_list": [],
                    },
                ),
                patch.object(
                    webhook_module,
                    "_send_twilio_message",
                    side_effect=RuntimeError("outbox unavailable"),
                ),
            ):
                result = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )

        self.assertEqual(result["completed"], 0, result)
        self.assertEqual(
            result["results"][0]["status"],
            WhatsAppInboundTurn.STATUS_RETRY_WAIT,
        )
        db.session.expire_all()
        self.assertEqual(
            WhatsAppInboundTurn.query.one().status,
            WhatsAppInboundTurn.STATUS_RETRY_WAIT,
        )
        self.assertEqual(WhatsAppOutboundAttempt.query.count(), 0)
        provider_client.messages.create.assert_not_called()

    def test_db_poller_runs_without_celery_and_drains_both_queues_once(self):
        self.app.config["WHATSAPP_INBOUND_CELERY_WAKEUP_ENABLED"] = False
        with (
            patch.object(worker_module, "summarize_whatsapp_turn_health", return_value={}),
            patch.object(
                worker_module,
                "process_whatsapp_inbound_stream",
                return_value={"processed": 1, "completed": 1},
            ) as inbound,
            patch.object(
                worker_module,
                "dispatch_whatsapp_outbound_attempts",
                return_value={"processed": 1, "accepted": 1},
            ) as outbound,
            patch.object(
                worker_module.process_whatsapp_inbound_stream_task,
                "apply_async",
            ) as apply_async,
        ):
            self.assertFalse(
                worker_module.enqueue_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key="stream-key",
                )
            )
            result = worker_module.run_whatsapp_durable_worker(
                self.app,
                once=True,
            )

        apply_async.assert_not_called()
        inbound.assert_called_once_with()
        outbound.assert_called_once_with()
        self.assertEqual(result["cycles"], 1)
        self.assertEqual(result["inbound_completed"], 1)
        self.assertEqual(result["outbound_accepted"], 1)

    def test_db_poller_refuses_to_run_when_queue_mode_is_disabled(self):
        self.app.config["WHATSAPP_INBOUND_DURABILITY_MODE"] = "legacy"

        with self.assertRaisesRegex(
            RuntimeError,
            "whatsapp_durable_worker_requires_queue_mode",
        ):
            worker_module.run_whatsapp_durable_worker(self.app, once=True)

    def test_worker_exception_logs_exclude_sql_provider_payloads_and_tracebacks(self):
        private_error = (
            "psycopg2 OperationalError SQL params phone=5492613168608 "
            "provider=https://api.twilio.test/messages?token=private-token"
        )

        with (
            patch.object(worker_module, "summarize_whatsapp_turn_health", return_value={}),
            patch.object(
                worker_module,
                "process_whatsapp_inbound_stream",
                side_effect=RuntimeError(private_error),
            ),
            self.assertLogs(worker_module.logger, level="ERROR") as captured,
        ):
            result = worker_module.run_whatsapp_durable_worker(self.app, once=True)

        self.assertEqual(result["cycles"], 1)
        rendered = "\n".join(captured.output)
        self.assertIn("RuntimeError", rendered)
        for private_value in (
            "psycopg2",
            "OperationalError",
            "SQL params",
            "5492613168608",
            "api.twilio.test",
            "private-token",
            "Traceback",
        ):
            self.assertNotIn(private_value, rendered)

    def test_signed_status_callback_reconciles_uncertain_attempt(self):
        payload = self._payload("SMqueuecallback001")
        with patch.object(webhook_module, "Client", return_value=MagicMock()):
            self.assertEqual(self._post(payload).status_code, 200)
            turn = WhatsAppInboundTurn.query.one()
            with patch.object(
                webhook_module,
                "responder_chatboc",
                return_value={
                    "message_body": "Respuesta pendiente de callback.",
                    "message_type": "text",
                    "options_list": [],
                },
            ):
                processed = worker_module.process_whatsapp_inbound_stream(
                    tenant_id=self.tenant.id,
                    stream_key=turn.stream_key,
                    limit=1,
                )
        self.assertEqual(processed["completed"], 1, processed)

        timeout_client = MagicMock()
        timeout_client.messages.create.side_effect = TimeoutError("provider timeout")
        with patch.object(worker_module, "Client", return_value=timeout_client):
            dispatch = worker_module.dispatch_next_whatsapp_outbound_attempt(
                tenant_id=self.tenant.id
            )
        self.assertEqual(dispatch.status, "send_uncertain")

        callback_path = (
            "/twilio/whatsapp/status?outbound_attempt_id="
            f"{dispatch.attempt_id}"
        )
        callback_url = f"http://localhost{callback_path}"
        callback_payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
            "MessageSid": "SMqueuecallbackprovider001",
            "MessageStatus": "delivered",
        }
        callback_signature = RequestValidator(CHILD_AUTH_TOKEN).compute_signature(
            callback_url,
            callback_payload,
        )

        response = self.client.post(
            callback_path,
            data=callback_payload,
            headers={"X-Twilio-Signature": callback_signature},
        )

        self.assertEqual(response.status_code, 200)
        db.session.expire_all()
        attempt = WhatsAppOutboundAttempt.query.filter_by(
            attempt_id=dispatch.attempt_id
        ).one()
        self.assertEqual(attempt.status, WhatsAppOutboundAttempt.STATUS_ACCEPTED)
        self.assertEqual(attempt.provider_status, "delivered")
        self.assertEqual(
            attempt.provider_message_sid,
            "SMqueuecallbackprovider001",
        )

    def test_signed_status_callback_returns_503_when_reconciliation_crashes(self):
        callback_path = (
            "/twilio/whatsapp/status?outbound_attempt_id="
            "00000000-0000-0000-0000-000000000001"
        )
        callback_url = f"http://localhost{callback_path}"
        callback_payload = {
            "AccountSid": CHILD_ACCOUNT_SID,
            "MessagingServiceSid": MESSAGING_SERVICE_SID,
            "From": f"whatsapp:{SENDER_NUMBER}",
            "To": f"whatsapp:{RECIPIENT_NUMBER}",
            "MessageSid": "SMqueuecallbackretry001",
            "MessageStatus": "delivered",
        }
        signature = RequestValidator(CHILD_AUTH_TOKEN).compute_signature(
            callback_url,
            callback_payload,
        )

        with patch(
            "services.whatsapp_inbound_turns.reconcile_whatsapp_outbound_status",
            side_effect=RuntimeError("database unavailable"),
        ):
            response = self.client.post(
                callback_path,
                data=callback_payload,
                headers={"X-Twilio-Signature": signature},
            )

        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
