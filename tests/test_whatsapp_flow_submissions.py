import json
import os
import unittest
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest


os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("SKIP_INIT_TENANTS", "1")

from app import create_app, db
from config import Config
from models import (
    ChatSessionContext,
    MessageTemplateRegistry,
    ProviderSender,
    TenantProfile,
    User,
    WhatsappNumero,
    WhatsAppFlowInteraction,
)
from services.whatsapp_flow_security import issue_whatsapp_flow_token
from services.whatsapp_flow_submissions import (
    CONTRACT_VERSION,
    MAX_FIELD_COUNT,
    MAX_LIST_ITEMS,
    MAX_SOURCE_BYTES,
    MAX_STRING_LENGTH,
    MAX_SYNTHETIC_TEXT_LENGTH,
    REDACTED_VALUE,
    FlowSubmissionValidationError,
    parse_whatsapp_flow_submission,
    safe_twilio_form_metadata,
)


def _wrapped_interactive_data(response: dict, *, name: str = "catalog_order") -> str:
    return json.dumps(
        {
            "type": "nfm_reply",
            "nfm_reply": {
                "name": name,
                "body": "Sent",
                "response_json": json.dumps(response),
            },
        }
    )


def test_parses_both_twilio_fields_and_redacts_nested_secrets_and_card_data():
    form = {
        "InteractiveData": _wrapped_interactive_data(
            {
                "flow_id": "catalog_order_builder",
                "flow_token": "raw-flow-token-never-store",
                "product": "Clavos galvanizados",
                "quantity": 20,
                "payment": {
                    "card_number": "4111 1111 1111 1111",
                    "cvv": "123",
                },
            }
        ),
        "FlowData": json.dumps(
            {
                "screen_id": "review",
                "delivery_note": "Retiro por mostrador",
            }
        ),
    }

    contract = parse_whatsapp_flow_submission(form, correlation_secret="test-correlation-secret")

    assert contract is not None
    assert contract["contract_version"] == CONTRACT_VERSION
    assert contract["flow"]["id"] == "catalog_order_builder"
    assert contract["flow"]["name"] == "catalog_order"
    assert contract["flow"]["screen"] == "review"
    assert contract["flow"]["recognized"] is None
    assert contract["correlation"]["token_present"] is True
    assert contract["correlation"]["digest_algorithm"] == "hmac-sha256"
    assert len(contract["correlation"]["token_digest"]) == 64
    response = contract["payload"]["interactive_data"]["nfm_reply"]["response_json"]
    assert response["flow_token"] == REDACTED_VALUE
    assert response["payment"]["card_number"] == REDACTED_VALUE
    assert response["payment"]["cvv"] == REDACTED_VALUE
    assert contract["integrity"]["redacted_field_count"] == 3
    assert contract["integrity"]["source_fields"] == ["InteractiveData", "FlowData"]

    persisted = json.dumps(contract, ensure_ascii=False)
    assert "raw-flow-token-never-store" not in persisted
    assert "4111 1111 1111 1111" not in persisted
    assert '"123"' not in persisted
    assert "Clavos galvanizados" in contract["synthetic_text"]
    assert "raw-flow-token-never-store" not in contract["synthetic_text"]


def test_redacts_camel_case_secrets_and_rejects_unregistered_flow_ids():
    with pytest.raises(FlowSubmissionValidationError) as exc_info:
        parse_whatsapp_flow_submission(
            {
                "InteractiveData": _wrapped_interactive_data(
                    {
                        "flow_id": "foreign_flow",
                        "flow_token": "correlation-token",
                        "accessToken": "access-secret",
                        "refreshToken": "refresh-secret",
                        "clientSecret": "client-secret",
                    }
                )
            },
            allowed_flow_ids={"catalog_order_builder"},
            correlation_secret="tenant-secret",
            require_correlation_token=True,
        )

    assert exc_info.value.code == "unrecognized_flow"

    contract = parse_whatsapp_flow_submission(
        {
            "InteractiveData": _wrapped_interactive_data(
                {
                    "flow_id": "catalog_order_builder",
                    "flow_token": "correlation-token",
                    "accessToken": "access-secret",
                    "refreshToken": "refresh-secret",
                    "clientSecret": "client-secret",
                }
            )
        },
        allowed_flow_ids={"catalog_order_builder"},
        correlation_secret="tenant-secret",
        require_correlation_token=True,
    )
    response = contract["payload"]["interactive_data"]["nfm_reply"]["response_json"]
    assert response["accessToken"] == REDACTED_VALUE
    assert response["refreshToken"] == REDACTED_VALUE
    assert response["clientSecret"] == REDACTED_VALUE
    assert contract["flow"]["recognized"] is True


def test_redacts_card_number_hidden_under_a_generic_field_name():
    contract = parse_whatsapp_flow_submission(
        {"FlowData": json.dumps({"reference": "4111111111111111", "method": "cash"})}
    )

    assert contract["payload"]["flow_data"]["reference"] == REDACTED_VALUE
    assert contract["payload"]["flow_data"]["method"] == "cash"


@pytest.mark.parametrize("source_field", ["InteractiveData", "FlowData"])
def test_rejects_non_object_roots(source_field):
    with pytest.raises(FlowSubmissionValidationError) as exc_info:
        parse_whatsapp_flow_submission({source_field: json.dumps(["not", "an", "object"])})

    assert exc_info.value.code == "root_must_be_object"
    assert exc_info.value.source_field == source_field


def test_rejects_invalid_embedded_response_json_instead_of_persisting_it_as_text():
    interactive = json.dumps(
        {
            "type": "nfm_reply",
            "nfm_reply": {
                "response_json": '{"flow_token":"secret-with-broken-json"',
            },
        }
    )

    with pytest.raises(FlowSubmissionValidationError) as exc_info:
        parse_whatsapp_flow_submission({"InteractiveData": interactive})

    assert exc_info.value.code == "invalid_embedded_json"
    assert "secret-with-broken-json" not in str(exc_info.value)


def test_enforces_source_size_depth_field_and_list_limits():
    with pytest.raises(FlowSubmissionValidationError) as size_error:
        parse_whatsapp_flow_submission(
            {"FlowData": json.dumps({"note": "x" * MAX_SOURCE_BYTES})}
        )
    assert size_error.value.code == "source_too_large"

    nested = {"answer": "ok"}
    for _ in range(8):
        nested = {"child": nested}
    with pytest.raises(FlowSubmissionValidationError) as depth_error:
        parse_whatsapp_flow_submission({"FlowData": json.dumps(nested)})
    assert depth_error.value.code == "nesting_depth_exceeded"

    too_many_fields = {f"field_{index}": index for index in range(MAX_FIELD_COUNT + 1)}
    with pytest.raises(FlowSubmissionValidationError) as fields_error:
        parse_whatsapp_flow_submission({"FlowData": json.dumps(too_many_fields)})
    assert fields_error.value.code == "field_count_exceeded"

    with pytest.raises(FlowSubmissionValidationError) as list_error:
        parse_whatsapp_flow_submission(
            {"FlowData": json.dumps({"choices": list(range(MAX_LIST_ITEMS + 1))})}
        )
    assert list_error.value.code == "list_item_count_exceeded"


def test_bounds_strings_and_synthetic_text_without_control_characters():
    contract = parse_whatsapp_flow_submission(
        {
            "FlowData": json.dumps(
                {
                    f"answer_{index}": f"value-{index}-" + ("z" * MAX_STRING_LENGTH)
                    for index in range(20)
                }
            )
        }
    )

    assert contract["integrity"]["truncated_field_count"] == 20
    assert len(contract["synthetic_text"]) <= MAX_SYNTHETIC_TEXT_LENGTH
    assert "\n" not in contract["synthetic_text"]
    assert "\r" not in contract["synthetic_text"]


def test_safe_request_metadata_contains_lengths_and_flags_only():
    metadata = safe_twilio_form_metadata(
        {
            "Body": "private customer message",
            "MessageSid": "SM-secret-id",
            "InteractiveData": '{"flow_token":"never-log-me"}',
            "NumMedia": "2",
        }
    )

    serialized = json.dumps(metadata)
    assert metadata["body_length"] == len("private customer message")
    assert metadata["media_count"] == 2
    assert metadata["has_interactive_data"] is True
    assert "private customer message" not in serialized
    assert "SM-secret-id" not in serialized
    assert "never-log-me" not in serialized


def test_signed_official_flow_response_uses_invocation_identity_and_data_allowlist():
    contract = parse_whatsapp_flow_submission(
        {
            "InteractiveData": json.dumps(
                {
                    "type": "nfm_reply",
                    "flowResponse": {
                        "flow_token": "signed-provider-token",
                        "catalog_items": [{"sku": "CLAVO-10", "quantity": 3}],
                        "unknown_admin_override": True,
                    },
                }
            )
        },
        allowed_flow_ids={"catalog_order_builder", "1232445823264765"},
        require_correlation_token=True,
        flow_token_validator=lambda _: {
            "interaction_id": 41,
            "token_digest": "a" * 64,
            "tenant_id": 7,
            "flow_id": "catalog_order_builder",
            "meta_flow_id": "1232445823264765",
            "provider_sender_id": 3,
            "data_contract": ["catalog_items"],
            "already_consumed": False,
            "expires_at": "2026-07-15T00:00:00+00:00",
        },
    )

    assert contract["flow"]["id"] == "catalog_order_builder"
    assert contract["flow"]["meta_id"] == "1232445823264765"
    assert contract["flow"]["recognized"] is True
    assert contract["payload"] == {
        "answers": {"catalog_items": [{"sku": "CLAVO-10", "quantity": 3}]}
    }
    assert contract["correlation"]["interaction_id"] == 41
    assert contract["integrity"]["signed_invocation_verified"] is True
    assert "unknown_admin_override" not in json.dumps(contract)
    assert "signed-provider-token" not in json.dumps(contract)


class _WebhookConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    WTF_CSRF_ENABLED = False
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True
    TWILIO_ACCOUNT_SID = "AC_flow_submission_test"
    TWILIO_AUTH_TOKEN = "twilio_flow_test_token"
    BACKEND_URL = "https://api.chatboc.test"
    WHATSAPP_FLOW_TOKEN_KEY_V1 = "test-whatsapp-flow-key-v1-00000000000000000000000000000000"
    WHATSAPP_FLOW_TOKEN_TTL_SECONDS = 3600


class WhatsAppFlowWebhookIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_WebhookConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.client = self.app.test_client()

        self.owner = User(
            name="Flow Owner",
            email="flow-owner@example.com",
            rol="empresa",
            tipo_chat="pyme",
            pyme_id=1,
            nombre_empresa="Flow Store",
        )
        self.owner.set_password("test-password")
        db.session.add(self.owner)
        db.session.flush()
        self.tenant = TenantProfile(
            slug="flow-store",
            nombre="Flow Store",
            tipo="pyme",
            pyme_id=self.owner.id,
            plan="full",
            configuracion={"whatsapp_phone": "+15551234567"},
        )
        db.session.add(self.tenant)
        db.session.flush()
        self.sender = ProviderSender(
            tenant_id=self.tenant.id,
            channel="whatsapp",
            phone_number="+15551234567",
            sender_id="whatsapp:+15551234567",
            status="active",
        )
        db.session.add(self.sender)
        db.session.flush()
        self.registry = MessageTemplateRegistry(
            tenant_id=self.tenant.id,
            provider="twilio",
            channel="whatsapp",
            name="catalog_order_builder_native_v1",
            language="es",
            category="UTILITY",
            status="approved",
            content_sid="HXflowtest",
            external_template_id="1232445823264765",
            metadata_json={
                "flow_id": "catalog_order_builder",
                "meta_flow_id": "1232445823264765",
                "content_family": "meta_native_flow",
                "approval_status": "approved",
                "meta_flow_status": "published",
                "data_contract": ["product", "quantity"],
            },
        )
        db.session.add(self.registry)
        db.session.flush()
        self.to_number = "+15551234567"
        self.from_number = "+15557654321"
        db.session.add(
            WhatsappNumero(
                numero_whatsapp=self.to_number,
                user_id=self.owner.id,
                is_active=True,
            )
        )
        db.session.add(
            ChatSessionContext(
                chat_session_id=f"whatsapp_{self.owner.id}_{self.from_number}",
                user_id=self.owner.id,
                tenant_id=self.tenant.id,
                anon_id=self.from_number,
                context_data={
                    "historial_chat": [],
                    "estado_conversacion": "activo",
                    "perfil_confirmado": True,
                    "awaiting_user_name": True,
                },
            )
        )
        db.session.commit()

        self.validator_patch = patch("routes.whatsapp_webhook.validator", MagicMock())
        self.validator = self.validator_patch.start()
        self.validator.validate.return_value = True
        self.twilio_patch = patch("routes.whatsapp_webhook.twilio_client", MagicMock())
        self.twilio_client = self.twilio_patch.start()
        self.twilio_client.messages.create.return_value = MagicMock(sid="SM_REPLY")

    def _issue_flow_token(self) -> str:
        issued = issue_whatsapp_flow_token(
            secret=self.app.config["WHATSAPP_FLOW_TOKEN_KEY_V1"],
            tenant_id=self.tenant.id,
            recipient=self.from_number,
            flow_id="catalog_order_builder",
            meta_flow_id="1232445823264765",
            provider_sender_id=self.sender.id,
            ttl_seconds=self.app.config["WHATSAPP_FLOW_TOKEN_TTL_SECONDS"],
        )
        db.session.add(
            WhatsAppFlowInteraction(
                tenant_id=self.tenant.id,
                template_registry_id=self.registry.id,
                provider_sender_id=self.sender.id,
                flow_id="catalog_order_builder",
                meta_flow_id="1232445823264765",
                content_sid="HXflowtest",
                recipient_hash=issued.recipient_hash,
                recipient_hint=issued.recipient_hint,
                token_digest=issued.token_digest,
                idempotency_key=f"test-{uuid4().hex}",
                status="sent",
                data_contract=["product", "quantity"],
                expires_at=issued.expires_at,
            )
        )
        db.session.commit()
        return issued.token

    def tearDown(self):
        self.validator_patch.stop()
        self.twilio_patch.stop()
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_flow_response_bypasses_welcome_persists_safe_contract_and_reaches_orchestrator(
        self,
        responder_chatboc,
    ):
        responder_chatboc.return_value = {
            "message_body": "Pedido recibido.",
            "message_type": "text",
            "options_list": [],
            "fuente": "flow_test",
            "skip_audio_generation": True,
        }
        raw_token = self._issue_flow_token()
        raw_card = "4111111111111111"
        payload = {
            "To": f"whatsapp:{self.to_number}",
            "From": f"whatsapp:{self.from_number}",
            "Body": "hola",
            "MessageSid": "SM_FLOW_INBOUND_1",
            "InteractiveData": json.dumps(
                {
                    "type": "nfm_reply",
                    "flowResponse": {
                    "flow_token": raw_token,
                    "product": "Clavos",
                    "quantity": 100,
                    "payment_reference": raw_card,
                    },
                }
            ),
            "FlowData": json.dumps({"screen_id": "review"}),
        }

        with patch("routes.whatsapp_webhook._log") as safe_log:
            response = self.client.post(
                "/webhook/whatsapp",
                data=payload,
                headers={"X-Twilio-Signature": "valid-test-signature"},
            )

        assert response.status_code == 200
        assert response.data.decode() == "OK"
        responder_chatboc.assert_called_once()
        bot_kwargs = responder_chatboc.call_args.kwargs
        assert bot_kwargs["pregunta"] != "hola"
        assert "formulario nativo" in bot_kwargs["pregunta"]
        assert bot_kwargs["whatsapp_flow_submission"]["contract_version"] == CONTRACT_VERSION

        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.owner.id}_{self.from_number}"
        ).first()
        persisted_contract = session.context_data["last_whatsapp_flow_submission"]
        serialized_context = json.dumps(session.context_data, ensure_ascii=False)
        assert persisted_contract["contract_version"] == CONTRACT_VERSION
        assert "synthetic_text" not in persisted_contract
        assert "synthetic_text" not in bot_kwargs["whatsapp_flow_submission"]
        assert raw_token not in serialized_context
        assert raw_card not in serialized_context
        assert session.context_data["awaiting_user_name"] is True

        serialized_log_calls = repr(safe_log.call_args_list)
        assert raw_token not in serialized_log_calls
        assert raw_card not in serialized_log_calls
        assert not any(
            call.kwargs.get("content_sid")
            for call in self.twilio_client.messages.create.call_args_list
        )

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_replayed_flow_token_with_new_message_sid_is_ignored(self, responder_chatboc):
        responder_chatboc.return_value = {
            "message_body": "Pedido recibido.",
            "message_type": "text",
            "options_list": [],
            "fuente": "flow_test",
            "skip_audio_generation": True,
        }
        raw_token = self._issue_flow_token()
        base_payload = {
            "To": f"whatsapp:{self.to_number}",
            "From": f"whatsapp:{self.from_number}",
            "Body": "",
            "InteractiveData": _wrapped_interactive_data(
                {
                    "flow_id": "catalog_order_builder",
                    "flow_token": raw_token,
                    "product": "Clavos",
                }
            ),
        }

        first = self.client.post(
            "/webhook/whatsapp",
            data={**base_payload, "MessageSid": "SM_FLOW_REPLAY_1"},
            headers={"X-Twilio-Signature": "valid-test-signature"},
        )
        second = self.client.post(
            "/webhook/whatsapp",
            data={**base_payload, "MessageSid": "SM_FLOW_REPLAY_2"},
            headers={"X-Twilio-Signature": "valid-test-signature"},
        )

        assert first.status_code == 200
        assert second.status_code == 200
        responder_chatboc.assert_called_once()
        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.owner.id}_{self.from_number}"
        ).one()
        assert len(session.context_data["processed_whatsapp_flow_token_digests"]) == 1

    @patch("routes.whatsapp_webhook.responder_chatboc")
    def test_malformed_flow_is_stopped_before_the_orchestrator(self, responder_chatboc):
        payload = {
            "To": f"whatsapp:{self.to_number}",
            "From": f"whatsapp:{self.from_number}",
            "Body": "hola",
            "MessageSid": "SM_FLOW_INVALID_1",
            "InteractiveData": json.dumps(
                {
                    "type": "nfm_reply",
                    "nfm_reply": {
                        "response_json": '{"flow_token":"must-not-reach-the-llm"',
                    },
                }
            ),
        }

        response = self.client.post(
            "/webhook/whatsapp",
            data=payload,
            headers={"X-Twilio-Signature": "valid-test-signature"},
        )

        assert response.status_code == 200
        assert response.data.decode() == "OK"
        responder_chatboc.assert_not_called()
        sent_bodies = [
            call.kwargs.get("body", "")
            for call in self.twilio_client.messages.create.call_args_list
        ]
        assert sent_bodies == [
            "No pudimos validar el formulario de WhatsApp. "
            "Volvelo a abrir y envialo nuevamente."
        ]

        session = ChatSessionContext.query.filter_by(
            chat_session_id=f"whatsapp_{self.owner.id}_{self.from_number}"
        ).first()
        serialized_context = json.dumps(session.context_data, ensure_ascii=False)
        assert "last_whatsapp_flow_submission" not in session.context_data
        assert "must-not-reach-the-llm" not in serialized_context
