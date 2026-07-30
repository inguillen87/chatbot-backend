from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

import jwt

from app import db
from models import AuditEvent, MessageTemplateRegistry, TenantProfile, User
from services.message_templates import (
    WHATSAPP_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE,
    render_whatsapp_template_preview,
    validate_whatsapp_template,
    whatsapp_template_lifecycle,
    whatsapp_template_pack_catalog,
)
from services.whatsapp_experience import (
    _local_twilio_manifest_template_map,
    _template_readiness_payload,
    _template_status,
)


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed(*, role: str = "admin", tenant_type: str = "municipio"):
    suffix = uuid4().hex[:10]
    user = User(
        email=f"wa-template-pack-{suffix}@test.com",
        name="Template operator",
        rol=role,
        tipo_chat=tenant_type,
    )
    user.set_password("pass")
    db.session.add(user)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"wa-template-pack-{suffix}",
        nombre="Template Pack Tenant",
        tipo=tenant_type,
        municipio_id=user.id if tenant_type == "municipio" else None,
        pyme_id=user.id if tenant_type != "municipio" else None,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    db.session.commit()
    return user, tenant


def test_versioned_vertical_packs_have_valid_rendered_previews():
    catalog = whatsapp_template_pack_catalog()

    assert catalog["contract_version"] == "whatsapp.template_pack.catalog.v1"
    assert catalog["provider_calls_performed"] is False
    assert catalog["summary"]["packs"] == 3
    assert catalog["summary"]["templates"] == 15
    assert set(catalog["lifecycle_states"]) == {
        "local_draft",
        "content_created",
        "approval_pending",
        "approved",
        "rejected",
        "stale",
    }
    packs = {pack["vertical"]: pack for pack in catalog["packs"]}
    assert set(packs) == {"municipio", "colegio", "empresa"}
    for pack in packs.values():
        assert pack["pack_version"] == "1.0.0"
        assert {item["intent"] for item in pack["templates"]} == {
            "confirmation",
            "follow_up",
            "appointment",
            "payment",
            "handoff",
        }
        for template in pack["templates"]:
            assert template["validation"] == {"valid": True, "errors": []}
            assert template["preview"]["contains_unresolved_variables"] is False
            assert "{{" not in template["preview"]["body"]
            assert template["lifecycle"]["state"] == "local_draft"
            assert template["lifecycle"]["production_send_allowed"] is False


def test_strict_validator_rejects_invalid_identity_variables_and_cta():
    invalid = {
        "intent": "payment",
        "name": "Bad Name",
        "language": "spanish",
        "category": "PROMO",
        "body": "Hola {nombre}, paga {{1}} y salta a {{3}}.",
        "variables": [
            {"index": 1, "name": "amount", "example": "$ 10"},
            {"index": 3, "name": "url", "example": "checkout/1"},
        ],
        "cta": {"type": "URL", "text": "Un titulo demasiado largo", "url": "http://user:pass@example.com/{{3}}"},
    }

    validation = validate_whatsapp_template(invalid)
    codes = {error["code"] for error in validation["errors"]}

    assert validation["valid"] is False
    assert {
        "invalid_template_name",
        "invalid_language",
        "invalid_category",
        "named_variables_not_allowed",
        "non_sequential_variables",
        "invalid_cta_text",
        "invalid_cta_url",
    } <= codes


def test_preview_replaces_body_and_cta_examples():
    template = {
        "intent": "confirmation",
        "name": "chatboc_test_confirmacion_v1",
        "language": "es_AR",
        "category": "UTILITY",
        "body": "Caso {{1}}: {{2}}.",
        "variables": [
            {"index": 1, "name": "case_code", "example": "REC-10"},
            {"index": 2, "name": "status", "example": "Ingresado"},
            {"index": 3, "name": "tracking_path", "example": "reclamos/REC-10"},
        ],
        "cta": {"type": "URL", "text": "Ver seguimiento", "url": "https://www.chatboc.ar/{{3}}"},
    }

    preview = render_whatsapp_template_preview(template)

    assert preview["body"] == "Caso REC-10: Ingresado."
    assert preview["cta"]["url"] == "https://www.chatboc.ar/reclamos/REC-10"
    assert preview["contains_unresolved_variables"] is False


def test_lifecycle_requires_fresh_provider_evidence_and_never_trusts_local_copy():
    now = datetime.now(timezone.utc)
    fresh = whatsapp_template_lifecycle(
        "approved",
        source="message_template_registry",
        provider_reference="HXtenantowned",
        observed_at=now,
        now=now,
    )
    expired = whatsapp_template_lifecycle(
        "approved",
        source="message_template_registry",
        provider_reference="HXtenantowned",
        observed_at=now - WHATSAPP_TEMPLATE_PROVIDER_EVIDENCE_MAX_AGE - timedelta(seconds=1),
        now=now,
    )
    local_active = whatsapp_template_lifecycle(
        "active",
        source="notification_template",
        provider_reference="HXuntrusted",
        observed_at=now,
        now=now,
    )

    assert fresh["state"] == "approved"
    assert fresh["production_send_allowed"] is True
    assert expired["state"] == "stale"
    assert expired["production_send_allowed"] is False
    assert local_active["state"] == "local_draft"
    assert local_active["production_send_allowed"] is False


def test_global_manifest_is_redacted_stale_and_not_remote_configured():
    manifest = _local_twilio_manifest_template_map()
    status = _template_status(manifest, "order_checkout")
    readiness = _template_readiness_payload(status, {})

    assert status["source"] == "local_twilio_manifest"
    assert status["reference_found"] is True
    assert status["local_configured"] is True
    assert status["remote_configured"] is False
    assert status["configured"] is False
    assert status["content_sid"] is None
    assert status["status"] == "stale"
    assert status["approved"] is False
    assert readiness["state"] == "stale"
    assert readiness["production_send_allowed"] is False


def test_chatboc_registry_row_cannot_forge_provider_approval():
    now = datetime.now(timezone.utc)
    status = _template_status(
        {
            "chatboc_empresa_pago_v1": {
                "source": "message_template_registry",
                "provider": "chatboc",
                "name": "chatboc_empresa_pago_v1",
                "status": "approved",
                "content_sid": "HXmustnotbetrusted",
                "last_sync_at": now.isoformat(),
            }
        },
        "chatboc_empresa_pago_v1",
    )

    assert status["status"] == "local_draft"
    assert status["approved"] is False
    assert status["remote_configured"] is False
    assert status["lifecycle"]["production_send_allowed"] is False


def test_template_selection_prefers_useful_fresh_lifecycle_over_expired_approved_row():
    now = datetime.now(timezone.utc)
    status = _template_status(
        {
            "chatboc_order_checkout_v1": {
                "source": "message_template_registry",
                "provider": "twilio",
                "name": "chatboc_order_checkout_v1",
                "status": "approved",
                "content_sid": "HXexpired",
                "last_sync_at": (now - timedelta(days=30)).isoformat(),
            },
            "chatboc_order_checkout_v2": {
                "source": "message_template_registry",
                "provider": "twilio",
                "name": "chatboc_order_checkout_v2",
                "status": "pending_approval",
                "content_sid": "HXpending",
                "last_sync_at": now.isoformat(),
            },
        },
        "order_checkout",
    )

    assert status["resolved_name"] == "chatboc_order_checkout_v2"
    assert status["status"] == "approval_pending"
    assert status["pending"] is True


def test_template_pack_endpoint_materializes_local_drafts_with_audit_and_idempotency(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    headers["Idempotency-Key"] = "municipio-pack-materialize-0001"

    with patch("routes.whatsapp_rules.Client") as twilio_client:
        catalog_response = client.get(
            "/api/admin/whatsapp/template-packs",
            headers=headers,
        )
        first = client.post(
            "/api/admin/whatsapp/template-packs/municipio/drafts",
            headers=headers,
            json={"pack_version": "1.0.0"},
        )
        replay = client.post(
            "/api/admin/whatsapp/template-packs/municipio/drafts",
            headers=headers,
            json={"pack_version": "1.0.0"},
        )

    assert catalog_response.status_code == 200
    catalog = catalog_response.get_json()
    assert catalog["tenant"] == {"id": tenant.id, "slug": tenant.slug}
    assert catalog["capabilities"]["materialize_local_draft"] is True
    assert catalog["provider_calls_performed"] is False

    assert first.status_code == 201, first.get_json()
    assert first.get_json()["created_count"] == 5
    assert first.get_json()["provider_calls_performed"] is False
    assert replay.status_code == 200
    assert replay.get_json()["idempotent_replay"] is True
    twilio_client.assert_not_called()

    rows = MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="chatboc",
        channel="whatsapp",
    ).all()
    assert len(rows) == 5
    assert {row.status for row in rows} == {"local_draft"}
    assert all(row.content_sid is None for row in rows)
    assert all(row.external_template_id is None for row in rows)
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="whatsapp_template_pack.local_drafts_materialized",
    ).count() == 1


def test_template_pack_mutation_is_capability_and_tenant_scoped(client, app):
    owner_a, tenant_a = _seed()
    owner_b, tenant_b = _seed(tenant_type="pyme")
    employee, _ = _seed(role="empleado")

    headers_a = _auth_headers(app, owner_a, tenant_a.slug)
    headers_a["Idempotency-Key"] = "tenant-a-template-pack-01"
    assert client.post(
        "/api/admin/whatsapp/template-packs/municipio/drafts",
        headers=headers_a,
        json={"pack_version": "1.0.0"},
    ).status_code == 201

    headers_b = _auth_headers(app, owner_b, tenant_b.slug)
    catalog_b = client.get("/api/admin/whatsapp/template-packs", headers=headers_b)
    assert catalog_b.status_code == 200
    municipio_b = next(
        pack for pack in catalog_b.get_json()["packs"] if pack["vertical"] == "municipio"
    )
    assert all(template["materialized"] is False for template in municipio_b["templates"])
    assert MessageTemplateRegistry.query.filter_by(tenant_id=tenant_b.id, provider="chatboc").count() == 0

    employee_headers = _auth_headers(app, employee, employee.tenant_slug)
    employee_headers["Idempotency-Key"] = "employee-template-pack-01"
    denied = client.post(
        "/api/admin/whatsapp/template-packs/empresa/drafts",
        headers=employee_headers,
        json={"pack_version": "1.0.0"},
    )
    assert denied.status_code == 403


def test_template_pack_idempotency_key_cannot_be_reused_for_another_operation(client, app):
    admin, tenant = _seed()
    headers = _auth_headers(app, admin, tenant.slug)
    headers["Idempotency-Key"] = "template-pack-conflict-0001"

    first = client.post(
        "/api/admin/whatsapp/template-packs/municipio/drafts",
        headers=headers,
        json={"pack_version": "1.0.0"},
    )
    conflict = client.post(
        "/api/admin/whatsapp/template-packs/colegio/drafts",
        headers=headers,
        json={"pack_version": "1.0.0"},
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert MessageTemplateRegistry.query.filter_by(
        tenant_id=tenant.id,
        provider="chatboc",
    ).count() == 5
