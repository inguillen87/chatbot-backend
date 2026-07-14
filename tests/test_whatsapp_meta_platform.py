from datetime import datetime, timedelta
import json
from types import SimpleNamespace
from unittest.mock import patch

import jwt

from app import db
from models import AuditEvent, MessageTemplateRegistry, ProviderConnection, ProviderSender, TenantProfile, User
from routes.whatsapp_rules import _sync_status_from_approval
from services.whatsapp_experience import build_whatsapp_experience


META_FLOW_ID = "1232445823264765"
FLOW_ID = "catalog_order_builder"


def test_rejected_or_disabled_approval_is_never_reported_pending():
    assert _sync_status_from_approval("rejected", submitted=True) == "rejected"
    assert _sync_status_from_approval("disabled", submitted=True) == "disabled"
    assert _sync_status_from_approval("unsubmitted", submitted=True) == "created"


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
    assert payload["flow"]["first_screen_id"] == "catalog"
    assert payload["flow"]["meta_flow_id"] == META_FLOW_ID

    create_request = payload["create_request"]
    assert create_request["variables"] == {"1": "session-demo-123456"}
    flow_type = create_request["types"]["whatsapp/flows"]
    assert flow_type["flow_id"] == META_FLOW_ID
    assert flow_type["flow_token"] == "{{1}}"
    assert flow_type["flow_first_page_id"] == "catalog"
    assert flow_type["is_flow_first_page_endpoint"] is False

    invalid = client.post(
        "/api/admin/whatsapp/flows/twilio-content/sync",
        headers=_auth_headers(app, admin, tenant.slug),
        json={"flow_id": FLOW_ID, "meta_flow_id": "demo-flow"},
    )
    assert invalid.status_code == 400


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
                assert data["friendly_name"] == "chatboc_catalog_order_builder_native_v1"
                assert data["types"]["whatsapp/flows"]["flow_id"] == META_FLOW_ID
                assert data["types"]["whatsapp/flows"]["flow_token"] == "{{1}}"
                return SimpleNamespace(status_code=201, text=json.dumps({"sid": "HXnativeflow"}))
            assert url == (
                "https://content.twilio.com/v1/Content/"
                "HXnativeflow/ApprovalRequests/whatsapp"
            )
            assert method == "POST"
            assert data == {
                "name": "chatboc_catalog_order_builder_native_v1",
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
        name="chatboc_catalog_order_builder_native_v1",
    ).one()
    assert row.external_template_id == META_FLOW_ID
    assert row.metadata_json["content_family"] == "meta_native_flow"
    assert row.metadata_json["first_screen_id"] == "catalog"
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
    app_config = {
        "TWILIO_META_APP_ID": "meta-app",
        "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": "meta-config",
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
        name="chatboc_catalog_order_builder_native_v1",
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
        },
    )
    db.session.add_all([sender, registry])
    db.session.commit()

    contract = build_whatsapp_experience(tenant, app_config=app_config)["meta_platform"]
    assert contract["active"] is True
    assert contract["sender"]["waba_id_present"] is True
    assert contract["native_flows"]["configured_count"] == 1
    assert contract["native_flows"]["active_count"] == 1
    submission_ingestion = contract["native_flows"]["submission_ingestion"]
    assert submission_ingestion["contract_version"] == "whatsapp.flow_submission.v1"
    assert submission_ingestion["status"] == "active"
    assert submission_ingestion["claimed_active"] is True
    assert submission_ingestion["rejects_invalid_payload_before_orchestration"] is True
    catalog_flow = next(item for item in contract["native_flows"]["flows"] if item["id"] == FLOW_ID)
    assert catalog_flow["active"] is True
    assert catalog_flow["meta_flow_id"] == META_FLOW_ID
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
