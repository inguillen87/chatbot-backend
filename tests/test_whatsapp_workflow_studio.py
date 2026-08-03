from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import jwt
import pytest

from app import db
from models import AuditEvent, Notification, TenantProfile, TenantTicket, User
from services.whatsapp_workflow_studio import build_workflow_studio_contract


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant_slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant_slug}


def _seed_tenant(*, slug: str, role: str = "admin") -> tuple[User, TenantProfile]:
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@test.com",
        rol=role,
        tenant_slug=slug,
        tipo_chat="pyme",
    )
    owner.set_password("secret")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()
    return owner, tenant


def _draft(*, trigger: dict | None = None) -> dict:
    return {
        "schema_version": "whatsapp.workflow_draft.v1",
        "name": "Ingreso multicanal",
        "trigger": trigger or {"type": "inbound_message"},
        "entry_step_id": "classify",
        "steps": [
            {
                "id": "classify",
                "type": "branch",
                "condition": {
                    "field": "message.type",
                    "operator": "equals",
                    "value": "image",
                },
                "on_true": "create_case",
                "on_false": "acknowledge",
            },
            {
                "id": "create_case",
                "type": "create_ticket",
                "category": "evidencia_visual",
            },
            {
                "id": "acknowledge",
                "type": "reply_template",
                "template_key": "workflow_acknowledge_v1",
            },
        ],
    }


def _codes(items: list[dict]) -> set[str]:
    return {str(item.get("code")) for item in items}


def test_workflow_contract_requires_an_explicit_tenant_scope():
    with pytest.raises(ValueError, match="tenant_scope_required"):
        build_workflow_studio_contract(tenant_id=1, tenant_slug="")
    with pytest.raises(ValueError, match="tenant_scope_required"):
        build_workflow_studio_contract(tenant_id=0, tenant_slug="demo")


def test_workflow_studio_contract_is_tenant_scoped_and_truthful(client, app):
    owner, tenant = _seed_tenant(slug="workflow-alpha")

    response = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio",
        headers=_auth_headers(app, owner, tenant.slug),
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["contract_version"] == "whatsapp.workflow_studio.v1"
    assert payload["tenant"] == {"id": tenant.id, "slug": tenant.slug}
    assert payload["capabilities"]["validate"]["available"] is True
    assert payload["capabilities"]["simulate"]["external_effects"] is False
    assert payload["capabilities"]["publish"]["available"] is False
    assert payload["capabilities"]["rollback"]["available"] is False
    assert payload["publication_readiness"]["ready"] is False
    assert "workflow_durable_storage_missing" in _codes(
        payload["publication_readiness"]["blockers"]
    )
    assert payload["side_effect_policy"]["provider_calls"] is False

    experience = client.get(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/experience",
        headers=_auth_headers(app, owner, tenant.slug),
    )
    assert experience.status_code == 200
    embedded = experience.get_json()["workflow_studio"]
    assert embedded["tenant"] == {"id": tenant.id, "slug": tenant.slug}
    assert embedded["frontend_contract"]["render_as"] == "workflow_studio_readiness"


def test_workflow_validation_normalizes_without_writes_and_is_deterministic(client, app):
    owner, tenant = _seed_tenant(slug="workflow-validate")
    headers = _auth_headers(app, owner, tenant.slug)
    before = {
        "audits": AuditEvent.query.count(),
        "tickets": TenantTicket.query.count(),
        "notifications": Notification.query.count(),
    }

    first = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/validate",
        headers=headers,
        json={"draft": _draft()},
    )
    second = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/validate",
        headers=headers,
        json={"draft": _draft()},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"
    first_payload = first.get_json()
    second_payload = second.get_json()
    assert first_payload["valid"] is True
    assert first_payload["simulation_allowed"] is True
    assert first_payload["publication"]["publishable"] is False
    assert first_payload["warnings"] == []
    assert first_payload["draft_id"] == second_payload["draft_id"]
    assert first_payload["draft_digest"] == second_payload["draft_digest"]
    first_deterministic = {key: value for key, value in first_payload.items() if key != "request_id"}
    second_deterministic = {key: value for key, value in second_payload.items() if key != "request_id"}
    assert first_deterministic == second_deterministic
    assert first_payload["side_effects"]["database_writes"] == 0
    assert before == {
        "audits": AuditEvent.query.count(),
        "tickets": TenantTicket.query.count(),
        "notifications": Notification.query.count(),
    }


def test_workflow_validation_fails_closed_for_spoofed_tenant_and_invalid_graph(client, app):
    owner, tenant = _seed_tenant(slug="workflow-invalid")
    draft = _draft()
    draft["tenant_id"] = tenant.id + 999
    draft["client_only"] = "must-not-be-ignored"
    for index in range(30):
        draft[f"unknown_{index:02d}"] = index
    draft["steps"][0]["on_true"] = "missing"
    draft["steps"][0]["on_false"] = "classify"
    draft["steps"][1]["provider_payload"] = {"unsafe": True}
    draft["steps"].append(
        {"id": "provider-send", "type": "provider_send", "recipient": "+549000"}
    )

    response = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/validate",
        headers=_auth_headers(app, owner, tenant.slug),
        json={"draft": draft},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["valid"] is False
    assert payload["simulation_allowed"] is False
    assert {
        "tenant_scope_override_forbidden",
        "unknown_fields_not_allowed",
        "unsupported_step_type",
        "dangling_step_reference",
        "workflow_cycle_not_allowed",
        "unreachable_steps",
    }.issubset(_codes(payload["blockers"]))
    root_unknown = next(
        blocker
        for blocker in payload["blockers"]
        if blocker["code"] == "unknown_fields_not_allowed" and blocker["path"] == "draft"
    )
    assert len(root_unknown["details"]["fields"]) == 20
    assert root_unknown["details"]["total"] >= 30
    assert root_unknown["details"]["truncated"] is True


def test_workflow_simulation_proposes_effects_but_executes_nothing(client, app):
    owner, tenant = _seed_tenant(slug="workflow-simulate")
    headers = _auth_headers(app, owner, tenant.slug)
    request_payload = {
        "draft": _draft(),
        "event": {"message": {"type": "image"}, "contact": {"language": "es"}},
    }
    before_tickets = TenantTicket.query.count()
    before_notifications = Notification.query.count()

    first = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json=request_payload,
    )
    second = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json=request_payload,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["Cache-Control"] == "no-store"
    assert second.headers["Cache-Control"] == "no-store"
    payload = first.get_json()
    assert payload["status"] == "completed"
    assert payload["trigger"] == {"matched": True, "reason": "inbound_message_matched"}
    assert payload["simulation_id"] == second.get_json()["simulation_id"]
    first_deterministic = {key: value for key, value in payload.items() if key != "request_id"}
    second_deterministic = {
        key: value for key, value in second.get_json().items() if key != "request_id"
    }
    assert first_deterministic == second_deterministic
    assert payload["trace"][0]["step_id"] == "classify"
    assert payload["trace"][0]["matched"] is True
    assert payload["proposed_effects"] == [
        {
            "type": "create_ticket",
            "category": "evidencia_visual",
            "execution": "proposed_only",
        }
    ]
    assert payload["side_effects"] == {
        "mode": "proposed_only",
        "database_writes": 0,
        "provider_calls": 0,
        "messages_sent": 0,
        "tickets_created": 0,
        "handoffs_created": 0,
    }
    assert TenantTicket.query.count() == before_tickets
    assert Notification.query.count() == before_notifications


def test_workflow_simulation_accepts_call_event_without_claiming_runtime_support(client, app):
    owner, tenant = _seed_tenant(slug="workflow-call")

    response = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=_auth_headers(app, owner, tenant.slug),
        json={"draft": _draft(), "event": {"message": {"type": "call"}}},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "completed"
    assert payload["proposed_effects"][0]["type"] == "reply_template"
    assert payload["side_effects"]["provider_calls"] == 0


def test_broadcast_reply_association_is_simulatable_but_runtime_binding_stays_blocked(client, app):
    owner, tenant = _seed_tenant(slug="workflow-broadcast")
    headers = _auth_headers(app, owner, tenant.slug)
    draft = _draft(trigger={"type": "broadcast_reply", "broadcast_id": "broadcast-2026-08"})

    not_matched = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json={
            "draft": draft,
            "event": {"message": {"type": "text", "text": "si"}, "broadcast_id": "other"},
        },
    )
    matched = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json={
            "draft": draft,
            "event": {
                "message": {"type": "text", "text": "si"},
                "broadcast_id": "broadcast-2026-08",
            },
        },
    )
    invalid_identifier = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json={
            "draft": draft,
            "event": {"message": {"type": "text"}, "broadcast_id": "unsafe id!"},
        },
    )

    assert not_matched.status_code == 200
    assert not_matched.get_json()["status"] == "not_triggered"
    assert not_matched.get_json()["proposed_effects"] == []
    assert matched.status_code == 200
    matched_payload = matched.get_json()
    assert matched_payload["trigger"]["matched"] is True
    assert "broadcast_reply_binding_missing" in _codes(
        matched_payload["publication"]["blockers"]
    )
    assert invalid_identifier.status_code == 422
    assert "invalid_broadcast_id" in _codes(invalid_identifier.get_json()["blockers"])


def test_invalid_simulation_is_422_and_cross_tenant_access_is_denied(client, app):
    owner, tenant = _seed_tenant(slug="workflow-owner")
    _, other_tenant = _seed_tenant(slug="workflow-other")
    headers = _auth_headers(app, owner, tenant.slug)

    blocked = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json={
            "draft": _draft(),
            "event": {
                "tenant_id": other_tenant.id,
                "provider_credentials": "must-not-be-accepted",
                "message": {"type": "text"},
            },
        },
    )
    forbidden = client.get(
        f"/api/v2/tenants/{other_tenant.slug}/whatsapp/workflow-studio",
        headers=headers,
    )
    forbidden_before_body_parse = client.post(
        f"/api/v2/tenants/{other_tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        data="this body must never be parsed for an unauthorized tenant",
        content_type="text/plain",
    )
    unknown_envelope = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/simulate",
        headers=headers,
        json={"draft": _draft(), "event": {"message": {"type": "text"}}, "execute": True},
    )
    wrong_content_type = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/validate",
        headers=headers,
        data=json.dumps({"draft": _draft()}),
        content_type="text/plain",
    )
    oversized = client.post(
        f"/api/v2/tenants/{tenant.slug}/whatsapp/workflow-studio/validate",
        headers=headers,
        data=json.dumps({"draft": {**_draft(), "padding": "x" * (70 * 1024)}}),
        content_type="application/json",
    )

    assert blocked.status_code == 422
    assert blocked.get_json()["status"] == "blocked"
    assert "tenant_scope_override_forbidden" in _codes(blocked.get_json()["blockers"])
    assert "unknown_fields_not_allowed" in _codes(blocked.get_json()["blockers"])
    assert blocked.get_json()["proposed_effects"] == []
    assert forbidden.status_code == 403
    assert forbidden.get_json()["reason_code"] == "forbidden_tenant"
    assert forbidden.headers["Cache-Control"] == "no-store"
    assert forbidden_before_body_parse.status_code == 403
    assert forbidden_before_body_parse.get_json()["reason_code"] == "forbidden_tenant"
    assert forbidden_before_body_parse.headers["Cache-Control"] == "no-store"
    assert unknown_envelope.status_code == 400
    assert unknown_envelope.get_json()["reason_code"] == "workflow_request_unknown_fields"
    assert wrong_content_type.status_code == 415
    assert wrong_content_type.get_json()["reason_code"] == "workflow_content_type_invalid"
    assert wrong_content_type.headers["Cache-Control"] == "no-store"
    assert oversized.status_code == 413
    assert oversized.get_json()["reason_code"] == "workflow_request_too_large"
    assert oversized.headers["Cache-Control"] == "no-store"
