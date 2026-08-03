from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app import db
from models import (
    AdminAuditLog,
    MessageTemplateRegistry,
    Notification,
    NotificationTemplate,
    TenantProfile,
    User,
)
from services.professional_message_preview import (
    PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
)


PREVIEW_PATH = "/api/admin/notifications/templates/preview"


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _seed_tenant(label: str, *, role: str = "admin"):
    suffix = uuid4().hex[:10]
    user = User(
        email=f"preview-api-{label}-{suffix}@test.com",
        name=f"Preview API {label}",
        rol=role,
        tipo_chat="pyme",
    )
    user.set_password("pass")
    db.session.add(user)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"preview-api-{label}-{suffix}",
        nombre=f"Preview API {label}",
        tipo="pyme",
        pyme_id=user.id,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    db.session.commit()
    return user, tenant


def _side_effect_counts(tenant_id: int) -> tuple[int, int]:
    return (
        Notification.query.filter_by(tenant_id=tenant_id).count(),
        AdminAuditLog.query.count(),
    )


def test_admin_preview_renders_twilio_snapshot_without_provider_or_db_side_effects(
    client,
    app,
    monkeypatch,
):
    admin, tenant = _seed_tenant("whatsapp")
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_claim_update_preview_v1",
        language="es_AR",
        category="UTILITY",
        status="approved",
        content_sid="HXpreviewonly",
        body_preview="Caso {{1}}: {{2}}.",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.flush()
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="claim_update_preview",
        channel="whatsapp",
        body_template="Actualizacion del caso ${claim_code}: ${status}.",
        is_active=True,
        message_template_registry_id=registry.id,
    )
    db.session.add(template)
    db.session.commit()

    def _provider_call_forbidden(*_args, **_kwargs):
        raise AssertionError("preview must not call Twilio")

    monkeypatch.setattr(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message",
        _provider_call_forbidden,
    )
    before = _side_effect_counts(tenant.id)

    response = client.post(
        PREVIEW_PATH,
        headers=_auth_headers(app, admin, tenant.slug),
        json={
            "template_id": template.id,
            "context": {
                "claim_code": "REC-10482",
                "status": "En tratamiento",
            },
            "content_variables": {
                "1": "REC-10482",
                "2": "En tratamiento",
            },
        },
    )

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["contract_version"] == PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION
    assert payload["tenant_id"] == tenant.id
    assert payload["rendered"]["body"] == (
        "Actualizacion del caso REC-10482: En tratamiento."
    )
    assert payload["provider_template"]["rendered_body"] == (
        "Caso REC-10482: En tratamiento."
    )
    assert payload["readiness"]["transport_readiness_checked"] is False
    assert payload["readiness"]["production_send_allowed"] is False
    assert payload["side_effects"] == {
        "provider_calls_performed": False,
        "messages_queued": 0,
        "messages_sent": 0,
    }
    assert _side_effect_counts(tenant.id) == before


def test_preview_key_channel_selector_is_exact_and_tenant_scoped(client, app):
    admin_a, tenant_a = _seed_tenant("tenant-a")
    admin_b, tenant_b = _seed_tenant("tenant-b")
    template_a = NotificationTemplate(
        tenant_id=tenant_a.id,
        key="claim_status",
        channel="in_app",
        body_template="Tenant A ${claim_code}",
    )
    template_b = NotificationTemplate(
        tenant_id=tenant_b.id,
        key="claim_status",
        channel="in_app",
        body_template="Tenant B ${claim_code}",
    )
    db.session.add_all([template_a, template_b])
    db.session.commit()
    headers_b = _auth_headers(app, admin_b, tenant_b.slug)
    before_a = _side_effect_counts(tenant_a.id)
    before_b = _side_effect_counts(tenant_b.id)

    own = client.post(
        PREVIEW_PATH,
        headers=headers_b,
        json={
            "key": "claim_status",
            "channel": "in_app",
            "context": {"claim_code": "REC-B"},
        },
    )
    assert own.status_code == 200
    assert own.get_json()["template"]["id"] == template_b.id
    assert own.get_json()["rendered"]["body"] == "Tenant B REC-B"

    cross_tenant = client.post(
        PREVIEW_PATH,
        headers=headers_b,
        json={
            "template_id": template_a.id,
            "context": {"claim_code": "private-cross-tenant-value"},
        },
    )
    assert cross_tenant.status_code == 404
    assert cross_tenant.headers["Cache-Control"] == "no-store"
    assert cross_tenant.get_json() == {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "error": "notification_template_not_found",
        "field": "template",
    }
    assert "private-cross-tenant-value" not in cross_tenant.get_data(as_text=True)
    assert _side_effect_counts(tenant_a.id) == before_a
    assert _side_effect_counts(tenant_b.id) == before_b


def test_preview_rejects_ambiguous_selector_and_redacts_context_values(client, app):
    admin, tenant = _seed_tenant("invalid-contract")
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="strict_preview",
        channel="email",
        subject_template="Caso ${claim_code}",
        body_template="Hola ${name}: ${claim_code}",
    )
    db.session.add(template)
    db.session.commit()
    headers = _auth_headers(app, admin, tenant.slug)
    before = _side_effect_counts(tenant.id)

    ambiguous = client.post(
        PREVIEW_PATH,
        headers=headers,
        json={
            "template_id": template.id,
            "key": template.key,
            "channel": template.channel,
            "context": {
                "name": "PII-NAME-MUST-NOT-LEAK",
                "claim_code": "REC-SECRET",
            },
        },
    )
    assert ambiguous.status_code == 400
    assert ambiguous.get_json()["error"] == "template_selector_invalid"
    assert "PII-NAME-MUST-NOT-LEAK" not in ambiguous.get_data(as_text=True)
    assert "REC-SECRET" not in ambiguous.get_data(as_text=True)

    missing_context = client.post(
        PREVIEW_PATH,
        headers=headers,
        json={
            "template_id": template.id,
            "context": {"name": "PII-NAME-MUST-NOT-LEAK"},
        },
    )
    assert missing_context.status_code == 400
    assert missing_context.get_json() == {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "error": "template_context_missing_variables",
        "field": "context",
        "variable_names": ["claim_code"],
    }
    assert "PII-NAME-MUST-NOT-LEAK" not in missing_context.get_data(as_text=True)
    assert _side_effect_counts(tenant.id) == before


def test_preview_requires_authenticated_admin_and_tenant_authorization(client, app):
    admin, tenant = _seed_tenant("auth-admin")
    viewer, viewer_tenant = _seed_tenant("auth-viewer", role="usuario")
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="auth_preview",
        channel="in_app",
        body_template="Vista segura",
    )
    db.session.add(template)
    db.session.commit()
    body = {"template_id": template.id, "context": {}}

    unauthenticated = client.post(
        PREVIEW_PATH,
        headers={"X-Tenant": tenant.slug},
        json=body,
    )
    assert unauthenticated.status_code == 401

    non_admin = client.post(
        PREVIEW_PATH,
        headers=_auth_headers(app, viewer, viewer_tenant.slug),
        json=body,
    )
    assert non_admin.status_code == 403

    wrong_tenant = client.post(
        PREVIEW_PATH,
        headers=_auth_headers(app, admin, viewer_tenant.slug),
        json=body,
    )
    assert wrong_tenant.status_code == 403
    assert Notification.query.count() == 0
    assert AdminAuditLog.query.count() == 0
