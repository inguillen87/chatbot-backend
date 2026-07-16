from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import db
from models import (
    AuditEvent,
    MessageTemplateRegistry,
    MessagingEventLedger,
    Order,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    User,
    WhatsAppEnterpriseRule,
    WhatsAppFlowInteraction,
)
from routes.whatsapp_rules import _flow_interaction_payload, _sync_status_from_approval
from services.meta_flow_json import build_order_checkout_flow
from services.whatsapp_experience import build_whatsapp_experience


META_FLOW_ID = "1232445823264765"
FLOW_ID = "order_checkout"
CONCEPTUAL_FLOW_ID = "catalog_order_builder"
ORDER_CONTEXT = {"kind": "order", "id": "order-flow-send"}


def test_rejected_or_disabled_approval_is_never_reported_pending():
    assert _sync_status_from_approval("rejected", submitted=True) == "rejected"
    assert _sync_status_from_approval("disabled", submitted=True) == "disabled"
    assert _sync_status_from_approval("unsubmitted", submitted=True) == "created"


def test_application_registers_meta_data_exchange_endpoint_fail_closed(client):
    response = client.post(
        "/api/whatsapp/flows/data-exchange/not-configured",
        json={},
    )

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "endpoint_not_found"


def test_flow_retry_contract_never_claims_same_key_retry_is_safe():
    payload = _flow_interaction_payload(
        SimpleNamespace(
            id=1,
            flow_id=FLOW_ID,
            meta_flow_id=META_FLOW_ID,
            content_sid="HXfailed",
            recipient_hint="***6799",
            status="failed",
            external_message_sid="SMFAILED",
            expires_at=None,
            consumed_at=None,
        )
    )

    assert payload["retry_safe"] is False
    assert payload["retry_mode"] == "new_invocation_after_review"
    assert payload["reconciliation_required"] is False


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed(*, plan: str = "full") -> tuple[User, TenantProfile]:
    admin = User(
        email=f"meta-ops-{plan}@test.com",
        name="Meta Ops Admin",
        rol="tenant_admin",
        tipo_chat="pyme",
    )
    admin.set_password("pass")
    db.session.add(admin)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"meta-ops-{plan}",
        nombre="Meta Ops",
        tipo="pyme",
        pyme_id=admin.id,
        plan=plan,
        configuracion={"whatsapp_phone": "+5491112345678"},
    )
    db.session.add(tenant)
    db.session.commit()
    return admin, tenant


def _prepare_ready_flow_send(app, tenant: TenantProfile):
    app.config["TWILIO_ACCOUNT_SID"] = "ACparent"
    app.config["TWILIO_AUTH_TOKEN"] = "parent-secret"
    app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN_ACFLOWSEND"] = "flow-send-secret"
    app.config["WHATSAPP_FLOW_TOKEN_KEY_V1"] = (
        "test-native-flow-send-key-v1-0000000000000000000000000000"
    )
    app.config["WHATSAPP_FLOW_TOKEN_TTL_SECONDS"] = 3600
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    app.config["PUBLIC_API_BASE_URL"] = "https://api.chatboc.test"
    app.config["META_FLOW_DATA_EXCHANGE_ENDPOINTS"] = {
        "flow-send-endpoint": {
            "endpoint_id": "flow-send-endpoint",
            "tenant_id": str(tenant.id),
            "waba_id": "waba-flow-send",
            "private_key_pem": private_key_pem,
            "app_secret": "meta-flow-send-test-secret",
            "handlers": {
                "init": lambda payload, context: {"screen": "ORDER_DETAILS", "data": {}},
                "back": lambda payload, context: {"screen": "ORDER_DETAILS", "data": {}},
                "data_exchange": lambda payload, context: {
                    "screen": "ORDER_CONFIRM",
                    "data": {"order_summary": "Pedido", "total_display": "$ 1"},
                },
            },
        }
    }
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="connected",
        external_account_id="ACflowsend",
        credentials_ref="env:TWILIO_SUBACCOUNT_AUTH_TOKEN_ACFLOWSEND",
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        phone_number="+5491100000000",
        sender_id="whatsapp:+5491100000000",
        waba_id="waba-flow-send",
        status="active",
        status_callback_url="https://api.chatboc.test/twilio/whatsapp/status",
        metadata_json={"meta_flow_data_exchange_endpoint_id": "flow-send-endpoint"},
    )
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HXflowtosend",
        external_template_id=META_FLOW_ID,
        body_preview="Completa tu pedido en WhatsApp.",
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "approval_status": "approved",
            "meta_flow_status": "published",
            "flow_json_sha256": build_order_checkout_flow().content_sha256,
            "meta_flow_publication_verified": True,
            "data_contract": ["catalog_items", "cart_id", "contact_key"],
        },
    )
    order = Order(
        id=ORDER_CONTEXT["id"],
        tenant_id=tenant.id,
        buyer_name="Cliente Flow",
        status="created",
        channel="whatsapp",
        currency="ARS",
        total=12500,
    )
    db.session.add_all([sender, registry, order])
    db.session.commit()
    return sender, registry


def test_native_flow_sync_dry_run_builds_exact_twilio_content_contract(client, app):
    admin, tenant = _seed()
    response = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["dry_run"] is True
    assert payload["ready_to_create"] is True
    assert payload["blocked"] is False
    assert payload["content_family"] == "meta_native_flow"
    assert isinstance(payload["execute_confirmation"], str)
    assert len(payload["execute_confirmation"]) > 40
    assert FLOW_ID not in payload["execute_confirmation"]
    assert payload["approval_request"]["category"] == "UTILITY"
    assert payload["flow"]["first_screen_id"] == "ORDER_DETAILS"
    assert payload["flow"]["flow_json_version"] == "7.3"
    assert payload["flow"]["data_api_version"] == "3.0"
    assert len(payload["flow"]["content_sha256"]) == 64
    assert payload["flow"]["meta_flow_json_upload_performed"] is False
    assert payload["flow"]["meta_flow_id"] == META_FLOW_ID

    create_request = payload["create_request"]
    assert create_request["variables"] == {"1": "session-demo-123456"}
    flow_type = create_request["types"]["whatsapp/flows"]
    assert flow_type["flow_id"] == META_FLOW_ID
    assert flow_type["flow_token"] == "{{1}}"
    assert flow_type["flow_first_page_id"] == "ORDER_DETAILS"
    assert flow_type["is_flow_first_page_endpoint"] is False

    invalid = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": FLOW_ID, "meta_flow_id": "demo-flow"},
    )
    assert invalid.status_code == 400


def test_native_flow_sync_rejects_conceptual_design_without_compiled_flow_json(client, app):
    admin, tenant = _seed()

    response = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": CONCEPTUAL_FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )

    assert response.status_code == 422
    assert "Flow JSON" in response.get_json()["message"]


def test_admin_can_download_exact_validated_meta_flow_json_artifact(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)

    response = client.get(
        f"/api/admin/whatsapp/flows/{FLOW_ID}/flow-json",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="{FLOW_ID}.flow.json"'
    )
    assert response.headers["X-Flow-JSON-Version"] == "7.3"
    assert response.headers["X-Flow-Data-API-Version"] == "3.0"
    content_sha256 = hashlib.sha256(response.data).hexdigest()
    assert response.headers["X-Flow-Content-SHA256"] == content_sha256
    assert response.headers["ETag"] == f'"sha256-{content_sha256}"'
    document = json.loads(response.data)
    assert document["version"] == "7.3"
    assert document["data_api_version"] == "3.0"
    assert [screen["id"] for screen in document["screens"]] == [
        "ORDER_DETAILS",
        "ORDER_CONFIRM",
    ]

    conceptual = client.get(
        f"/api/admin/whatsapp/flows/{CONCEPTUAL_FLOW_ID}/flow-json",
        headers=headers,
    )
    assert conceptual.status_code == 404


def test_native_flow_sync_requires_full_plan_and_never_issues_execute_token_when_locked(client, app):
    admin, tenant = _seed(plan="free")
    headers = _auth_headers(app, admin, tenant.slug)

    preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["blocked"] is True
    assert preview_payload["ready_to_create"] is False
    assert "execute_confirmation" not in preview_payload
    assert preview_payload["operator_guardrails"]["twilio_call_allowed"] is False

    with patch("routes.whatsapp_rules.Client") as client_factory:
        execute = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "meta_flow_id": META_FLOW_ID,
                "dry_run": False,
                "execute_confirmation": f"sync_twilio_flow:{FLOW_ID}:{META_FLOW_ID}",
            },
        )
    assert execute.status_code == 403
    assert execute.get_json()["action"] == "create_twilio_native_flow"
    client_factory.assert_not_called()


def test_native_flow_sync_creates_registry_audit_and_is_idempotent(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    app.config["TWILIO_ACCOUNT_SID"] = "ACparent"
    app.config["TWILIO_AUTH_TOKEN"] = "parent-secret"
    app.config["TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS"] = "tenant-secret"
    tenant.configuracion = {
        **tenant.configuracion,
        "twilio_tech_provider": {
            "twilio_account_sid": "ACmetaops",
            "twilio_subaccount_token_ref": "TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS",
        },
    }
    db.session.add(
        ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="connected",
            external_account_id="ACmetaops",
            credentials_ref="env:TWILIO_SUBACCOUNT_AUTH_TOKEN_ACMETAOPS",
        )
    )
    db.session.commit()

    class _FakeClient:
        def __init__(self):
            self.calls = []

        def request(self, method, url, *, data, headers, timeout):
            self.calls.append((method, url, data, headers, timeout))
            if url == "https://content.twilio.com/v1/Content":
                assert method == "POST"
                assert data["friendly_name"] == "chatboc_order_checkout_native_v1"
                assert data["types"]["whatsapp/flows"]["flow_id"] == META_FLOW_ID
                assert data["types"]["whatsapp/flows"]["flow_token"] == "{{1}}"
                return SimpleNamespace(status_code=201, text=json.dumps({"sid": "HXnativeflow"}))
            assert url == (
                "https://content.twilio.com/v1/Content/"
                "HXnativeflow/ApprovalRequests/whatsapp"
            )
            assert method == "POST"
            assert data == {
                "name": "chatboc_order_checkout_native_v1",
                "category": "UTILITY",
            }
            return SimpleNamespace(status_code=201, text=json.dumps({"status": "PENDING"}))

    fake_client = _FakeClient()

    missing_confirmation = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID, "dry_run": False},
    )
    assert missing_confirmation.status_code == 409

    preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
    ).get_json()

    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        response = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={
                "flow_id": FLOW_ID,
                "meta_flow_id": META_FLOW_ID,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["content_sid"] == "HXnativeflow"
    assert payload["registry"]["status"] == "pending_approval"
    client_factory.assert_called_once_with("ACmetaops", "tenant-secret")

    row = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
    ).one()
    assert row.external_template_id == META_FLOW_ID
    assert row.metadata_json["content_family"] == "meta_native_flow"
    assert row.metadata_json["first_screen_id"] == "ORDER_DETAILS"
    assert row.metadata_json["flow_json_version"] == "7.3"
    assert row.metadata_json["data_api_version"] == "3.0"
    assert len(row.metadata_json["flow_json_sha256"]) == 64
    assert row.metadata_json["meta_flow_json_upload_performed"] is False
    assert row.metadata_json["meta_flow_publication_verified"] is False
    audit = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="whatsapp_flow.twilio_content_synced",
        resource_id=str(row.id),
    ).one()
    assert audit.details["meta_flow_id"] == META_FLOW_ID
    assert audit.details["twilio_account_scope"] == "subaccount"
    assert len(fake_client.calls) == 2

    with patch("routes.whatsapp_rules.Client") as duplicate_client:
        duplicate = client.post(
            "/api/admin/whatsapp/flows/twilio-content/sync",
            headers=headers,
            json={"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID, "dry_run": False},
        )
    assert duplicate.status_code == 200
    assert duplicate.get_json()["reason"] == "already_registered"
    duplicate_client.assert_not_called()

    replacement_meta_flow_id = "987654321012345"
    conflict = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": replacement_meta_flow_id},
    ).get_json()
    assert conflict["conflict"] is True
    assert conflict["ready_to_create"] is False
    assert "execute_confirmation" not in conflict

    forced_preview = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=headers,
        json={"flow_id": FLOW_ID, "meta_flow_id": replacement_meta_flow_id, "force": True},
    ).get_json()
    assert forced_preview["conflict"] is True
    assert forced_preview["forced_replacement"] is True
    assert forced_preview["ready_to_create"] is True
    assert isinstance(forced_preview["execute_confirmation"], str)
    assert len(forced_preview["execute_confirmation"]) > 40
    assert replacement_meta_flow_id not in forced_preview["execute_confirmation"]


def test_meta_platform_contract_only_activates_persisted_capabilities(client, app):
    _, tenant = _seed()
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    artifact = build_order_checkout_flow()

    def init_handler(payload, context):
        return {"screen": "ORDER_DETAILS", "data": {}}

    def back_handler(payload, context):
        return {"screen": "ORDER_DETAILS", "data": {}}

    def exchange_handler(payload, context):
        return {
            "screen": "ORDER_CONFIRM",
            "data": {"order_summary": "2 productos", "total_display": "$ 25.000"},
        }

    app_config = {
        "TWILIO_META_APP_ID": "meta-app",
        "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": "meta-config",
        "WHATSAPP_FLOW_TOKEN_KEY_V1": "test-flow-key-v1-000000000000000000000000000000000000",
        "PUBLIC_API_BASE_URL": "https://api.chatboc.test",
        "META_FLOW_DATA_EXCHANGE_ENDPOINTS": {
            "123456789": {
                "endpoint_id": "123456789",
                "tenant_id": str(tenant.id),
                "waba_id": "123456789",
                "private_key_pem": private_key_pem,
                "app_secret": "meta-test-app-secret",
                "handlers": {
                    "init": init_handler,
                    "back": back_handler,
                    "data_exchange": exchange_handler,
                },
            }
        },
    }

    initial = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert initial["active"] is False
    assert initial["native_flows"]["configured"] is False
    assert initial["native_flows"]["active"] is False
    assert initial["business_calling"]["active"] is False
    assert initial["catalog"]["active"] is False
    assert initial["embedded_signup"]["tenant_completed"] is False
    assert initial["embedded_signup"]["status"] == "ready_to_start"

    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        phone_number="+5491112345678",
        sender_sid="XEwhatsappsender",
        waba_id="123456789",
        phone_number_id="987654321",
        status="active",
        metadata_json={
            "whatsapp_business_calling_status": "approved",
            "user_initiated_calling_enabled": True,
            "business_initiated_calling_enabled": False,
            "catalog_id": "catalog-123",
            "catalog_status": "active",
        },
    )
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_order_checkout_native_v1",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HXnativeflow",
        external_template_id=META_FLOW_ID,
        metadata_json={
            "flow_id": FLOW_ID,
            "meta_flow_id": META_FLOW_ID,
            "content_family": "meta_native_flow",
            "approval_status": "approved",
            "meta_flow_status": "published",
            "flow_json_sha256": artifact.content_sha256,
            "meta_flow_publication_verified": True,
        },
    )
    db.session.add_all([sender, registry])
    db.session.commit()

    contract = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert contract["active"] is True
    assert contract["sender"]["waba_id_present"] is True
    assert contract["native_flows"]["configured_count"] == 1
    assert contract["native_flows"]["active_count"] == 1
    assert contract["native_flows"]["data_exchange"]["ready"] is True
    assert contract["native_flows"]["send_endpoint"] == "/api/admin/whatsapp/flows/send"
    assert contract["native_flows"]["security"]["dedicated_token_key_ready"] is True
    submission_ingestion = contract["native_flows"]["submission_ingestion"]
    assert submission_ingestion["contract_version"] == "whatsapp.twilio_flow_completion.v1"
    assert submission_ingestion["is_meta_data_exchange_endpoint"] is False
    assert submission_ingestion["status"] == "active"
    assert submission_ingestion["claimed_active"] is True
    assert submission_ingestion["rejects_invalid_payload_before_orchestration"] is True
    catalog_flow = next(item for item in contract["native_flows"]["flows"] if item["id"] == FLOW_ID)
    assert catalog_flow["active"] is True
    assert catalog_flow["meta_flow_id"] == META_FLOW_ID
    assert catalog_flow["artifact_identity_verified"] is True
    assert catalog_flow["meta_flow_publication_verified"] is True
    assert contract["business_calling"]["active"] is True
    assert contract["business_calling"]["requires_explicit_user_consent"] is True
    assert contract["business_calling"]["whatsapp_pstn_bridge_allowed"] is False
    assert contract["catalog"]["active"] is True
    assert contract["embedded_signup"]["platform_configured"] is True
    assert contract["embedded_signup"]["tenant_completed"] is True
    assert contract["embedded_signup"]["active"] is True

    sender.status = "registered"
    db.session.add(sender)
    db.session.commit()
    pending_sender = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert pending_sender["sender"]["ready"] is False
    assert pending_sender["native_flows"]["active_count"] == 0
    assert pending_sender["native_flows"]["submission_ingestion"]["claimed_active"] is False


def test_native_flow_send_preview_execute_and_idempotent_replay(client, app):
    admin, tenant = _seed()
    sender, registry = _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    request_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+54 9 11 2345-6789",
        "idempotency_key": "flow-send-idem-001",
        "order_context": ORDER_CONTEXT,
    }

    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=request_payload,
    )
    assert preview.status_code == 200
    preview_payload = preview.get_json()
    assert preview_payload["ready_to_send"] is True
    assert preview_payload["blockers"] == []
    assert preview_payload["recipient_hint"] == "***6789"
    assert preview_payload["provider_sender_id"] == sender.id
    assert preview_payload["content_sid"] == registry.content_sid
    assert preview_payload["security"]["one_time_token"] is True
    assert preview_payload["security"]["token_exposed"] is False
    assert isinstance(preview_payload["execute_confirmation"], str)
    serialized_preview = json.dumps(preview_payload)
    assert "+5491123456789" not in serialized_preview
    assert "flow_token" not in serialized_preview

    class _Messages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(sid="SMNATIVEFLOW001")

    messages = _Messages()
    fake_client = SimpleNamespace(messages=messages)
    with patch("routes.whatsapp_rules.Client", return_value=fake_client) as client_factory:
        execute = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **request_payload,
                "dry_run": False,
                "execute_confirmation": preview_payload["execute_confirmation"],
            },
        )
    assert execute.status_code == 201
    execute_payload = execute.get_json()
    assert execute_payload["sent"] is True
    assert execute_payload["interaction"]["status"] == "sent"
    assert execute_payload["interaction"]["recipient_hint"] == "***6789"
    assert "token" not in json.dumps(execute_payload).lower()
    client_factory.assert_called_once_with("ACflowsend", "flow-send-secret")
    assert len(messages.calls) == 1
    sent = messages.calls[0]
    assert sent["to"] == "whatsapp:+5491123456789"
    assert sent["from_"] == "whatsapp:+5491100000000"
    assert sent["content_sid"] == "HXflowtosend"
    callback = urlsplit(sent["status_callback"])
    assert callback.scheme == "https"
    assert callback.netloc == "api.chatboc.test"
    assert callback.path == "/twilio/whatsapp/status"
    assert parse_qs(callback.query)["flow_interaction_id"] == [
        str(execute_payload["interaction"]["id"])
    ]
    provider_token = json.loads(sent["content_variables"])["1"]
    assert len(provider_token) > 80
    assert "+5491123456789" not in provider_token

    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key="flow-send-idem-001",
    ).one()
    assert interaction.token_digest != provider_token
    assert interaction.recipient_hint == "***6789"
    assert interaction.metadata_json["order_context"] == ORDER_CONTEXT
    assert "+5491123456789" not in json.dumps(interaction.metadata_json)
    event = MessagingEventLedger.query.filter_by(
        tenant_id=tenant.id,
        event_type="native_flow_sent",
    ).one()
    assert event.external_message_sid == "SMNATIVEFLOW001"
    assert event.recipient == "***6789"

    with patch("routes.whatsapp_rules.Client") as replay_client:
        replay = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={**request_payload, "dry_run": False},
        )
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    replay_client.assert_not_called()


def test_native_flow_send_timeout_is_uncertain_and_never_auto_retries(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    request_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+5491123456790",
        "idempotency_key": "flow-send-timeout-001",
        "order_context": ORDER_CONTEXT,
    }
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=request_payload,
    ).get_json()
    messages = SimpleNamespace(create=MagicMock(side_effect=TimeoutError("provider timeout")))
    with patch(
        "routes.whatsapp_rules.Client",
        return_value=SimpleNamespace(messages=messages),
    ):
        execute = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **request_payload,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )
    assert execute.status_code == 502
    interaction = WhatsAppFlowInteraction.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key="flow-send-timeout-001",
    ).one()
    assert interaction.status == "send_uncertain"
    assert interaction.error_code == "TimeoutError"

    with patch("routes.whatsapp_rules.Client") as retry_client:
        retry = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={**request_payload, "dry_run": False},
        )
    assert retry.status_code == 409
    assert retry.get_json()["interaction"]["retry_safe"] is False
    retry_client.assert_not_called()


def test_native_flow_send_fails_closed_without_dedicated_key(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    app.config["WHATSAPP_FLOW_TOKEN_KEY_V1"] = ""
    headers = _auth_headers(app, admin, tenant.slug)
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456791",
            "idempotency_key": "flow-send-key-missing",
        },
    )
    assert preview.status_code == 200
    payload = preview.get_json()
    assert payload["ready_to_send"] is False
    assert "flow_token_key_not_configured" in payload["blockers"]
    assert "execute_confirmation" not in payload


def test_order_flow_send_requires_tenant_owned_order_context(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)

    missing = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456797",
            "idempotency_key": "flow-order-context-missing",
        },
    ).get_json()
    unavailable = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456797",
            "idempotency_key": "flow-order-context-other",
            "order_context": {"kind": "order", "id": "another-tenant-order"},
        },
    ).get_json()

    assert missing["ready_to_send"] is False
    assert "order_context_missing" in missing["blockers"]
    assert unavailable["ready_to_send"] is False
    assert "order_context_unavailable" in unavailable["blockers"]
    assert "execute_confirmation" not in missing
    assert "execute_confirmation" not in unavailable


def test_native_flow_send_requires_verified_publication_and_data_exchange(client, app):
    admin, tenant = _seed()
    _, registry = _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)
    metadata = dict(registry.metadata_json)
    metadata["meta_flow_publication_verified"] = False
    registry.metadata_json = metadata
    db.session.add(registry)
    db.session.commit()

    publication_blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456798",
            "idempotency_key": "flow-publication-missing-001",
        },
    ).get_json()
    assert publication_blocked["ready_to_send"] is False
    assert "meta_flow_publication_not_verified" in publication_blocked["blockers"]

    metadata["meta_flow_publication_verified"] = True
    registry.metadata_json = metadata
    app.config["META_FLOW_DATA_EXCHANGE_ENDPOINTS"] = {}
    db.session.add(registry)
    db.session.commit()
    endpoint_blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456798",
            "idempotency_key": "flow-endpoint-missing-001",
        },
    ).get_json()
    assert endpoint_blocked["ready_to_send"] is False
    assert "data_exchange_not_ready" in endpoint_blocked["blockers"]


def test_native_flow_send_requires_explicit_e164_and_boolean_dry_run(client, app):
    admin, tenant = _seed()
    _prepare_ready_flow_send(app, tenant)
    headers = _auth_headers(app, admin, tenant.slug)

    invalid_phone = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "5491123456792",
            "idempotency_key": "flow-invalid-e164-001",
        },
    )
    invalid_boolean = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456792",
            "idempotency_key": "flow-invalid-dry-run-001",
            "dry_run": "false",
        },
    )
    repeated_plus = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+54+91123456792",
            "idempotency_key": "flow-invalid-e164-plus-001",
        },
    )

    assert invalid_phone.status_code == 400
    assert invalid_boolean.status_code == 400
    assert repeated_plus.status_code == 400


def test_twilio_sync_rejects_string_boolean_controls(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    requests = (
        (
            "/api/admin/templates/twilio-content/sync",
            {"template_id": "order_checkout"},
        ),
        (
            "/api/admin/whatsapp/flows/twilio-content/sync",
            {"flow_id": FLOW_ID, "meta_flow_id": META_FLOW_ID},
        ),
    )

    for endpoint, base_payload in requests:
        for field in ("submit_for_approval", "force"):
            response = client.post(
                endpoint,
                headers=headers,
                json={**base_payload, field: "false"},
            )
            assert response.status_code == 400
            assert field in response.get_json()["message"]


def test_native_flow_send_counts_durable_invocations_against_hourly_limit(client, app):
    admin, tenant = _seed()
    first_sender, _ = _prepare_ready_flow_send(app, tenant)
    second_sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=first_sender.provider_connection_id,
        channel="whatsapp",
        phone_number="+5491100000001",
        sender_id="whatsapp:+5491100000001",
        waba_id="waba-flow-send",
        status="active",
        status_callback_url="https://api.chatboc.test/twilio/whatsapp/status",
        metadata_json={"meta_flow_data_exchange_endpoint_id": "flow-send-endpoint"},
    )
    db.session.add(second_sender)
    headers = _auth_headers(app, admin, tenant.slug)
    db.session.add(
        WhatsAppEnterpriseRule(
            tenant_id=tenant.id,
            max_outbound_per_hour=1,
        )
    )
    db.session.commit()
    first_payload = {
        "flow_id": FLOW_ID,
        "recipient": "+5491123456793",
        "idempotency_key": "flow-hourly-limit-001",
        "order_context": ORDER_CONTEXT,
    }
    preview = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json=first_payload,
    ).get_json()
    messages = SimpleNamespace(create=MagicMock(return_value=SimpleNamespace(sid="SMFLOWLIMIT001")))
    with patch("routes.whatsapp_rules.Client", return_value=SimpleNamespace(messages=messages)):
        sent = client.post(
            "/api/admin/whatsapp/flows/send",
            headers=headers,
            json={
                **first_payload,
                "dry_run": False,
                "execute_confirmation": preview["execute_confirmation"],
            },
        )

    blocked = client.post(
        "/api/admin/whatsapp/flows/send",
        headers=headers,
        json={
            "flow_id": FLOW_ID,
            "recipient": "+5491123456794",
            "idempotency_key": "flow-hourly-limit-002",
            "provider_sender_id": second_sender.id,
            "order_context": ORDER_CONTEXT,
        },
    )

    assert sent.status_code == 201
    assert blocked.status_code == 200
    assert blocked.get_json()["ready_to_send"] is False
    assert "rate_limited" in blocked.get_json()["blockers"]
    messages.create.assert_called_once()
