from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app import db
from models import MessageTemplateRegistry, NotificationTemplate, TenantProfile, User
from services.professional_message_preview import (
    PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
    ProfessionalMessageContractError,
    preview_notification_template,
    render_notification_template_strict,
)


@pytest.fixture
def app_context(app):
    with app.app_context():
        # Other endpoint fixtures intentionally drop the shared SQLite schema
        # during teardown. Recreate it here so this service-level suite is
        # deterministic regardless of collection or execution order.
        db.create_all()
        try:
            yield
        finally:
            db.session.remove()


def _seed_tenant(label: str):
    suffix = uuid4().hex[:10]
    user = User(
        email=f"professional-preview-{label}-{suffix}@test.com",
        name=f"Preview {label}",
        rol="admin",
        tipo_chat="pyme",
    )
    user.set_password("pass")
    db.session.add(user)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"professional-preview-{label}-{suffix}",
        nombre=f"Preview {label}",
        tipo="pyme",
        pyme_id=user.id,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    db.session.commit()
    return user, tenant


def test_strict_renderer_resolves_subject_and_body_with_exact_contract():
    preview = render_notification_template_strict(
        subject_template="Novedad del caso ${claim_code}",
        body_template="Hola ${name}. El caso ${claim_code} está en ${status}.",
        context={
            "name": "Ana",
            "claim_code": "REC-10482",
            "status": "En tratamiento",
        },
        channel="email",
    )

    assert preview == {
        "body": "Hola Ana. El caso REC-10482 está en En tratamiento.",
        "subject": "Novedad del caso REC-10482",
        "required_variables": ["claim_code", "name", "status"],
        "received_variables": ["claim_code", "name", "status"],
        "strict_variable_contract": True,
        "contains_unresolved_variables": False,
    }


@pytest.mark.parametrize(
    ("context", "expected_code", "expected_names"),
    [
        ({"name": "Ana"}, "template_context_missing_variables", ["claim_code"]),
        (
            {"name": "Ana", "claim_code": "REC-1", "unused": "no enviar"},
            "template_context_unexpected_variables",
            ["unused"],
        ),
    ],
)
def test_strict_renderer_rejects_missing_and_unexpected_variables_without_values(
    context, expected_code, expected_names
):
    with pytest.raises(ProfessionalMessageContractError) as captured:
        render_notification_template_strict(
            body_template="Hola ${name}; caso ${claim_code}",
            context=context,
            channel="whatsapp",
        )

    assert captured.value.to_dict() == {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "error": expected_code,
        "field": "context",
        "variable_names": expected_names,
    }
    assert "Ana" not in str(captured.value.to_dict())
    assert "REC-1" not in str(captured.value.to_dict())


def test_strict_renderer_rejects_malformed_placeholder():
    with pytest.raises(ProfessionalMessageContractError) as captured:
        render_notification_template_strict(
            body_template="Hola $9 y ${name}",
            context={"name": "Ana"},
            channel="in_app",
        )

    assert captured.value.code == "template_placeholder_invalid"
    assert captured.value.field == "body_template"


def test_strict_renderer_rejects_non_string_template_source():
    with pytest.raises(ProfessionalMessageContractError) as captured:
        render_notification_template_strict(
            body_template={"text": "Hola"},
            context={},
            channel="in_app",
        )

    assert captured.value.to_dict() == {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "error": "template_source_must_be_string",
        "field": "body_template",
    }


def test_preview_lookup_is_tenant_scoped_and_does_not_leak_cross_tenant_template(app_context):
    _, tenant_a = _seed_tenant("a")
    _, tenant_b = _seed_tenant("b")
    template = NotificationTemplate(
        tenant_id=tenant_a.id,
        key="claim_update",
        channel="in_app",
        body_template="Caso ${claim_code}: ${status}",
        is_active=True,
    )
    db.session.add(template)
    db.session.commit()

    with pytest.raises(ProfessionalMessageContractError) as captured:
        preview_notification_template(
            tenant_id=tenant_b.id,
            template_id=template.id,
            context={"claim_code": "REC-1", "status": "Ingresado"},
        )

    assert captured.value.to_dict()["error"] == "notification_template_not_found"


def test_whatsapp_preview_renders_provider_contract_but_never_certifies_transport(app_context):
    _, tenant = _seed_tenant("whatsapp")
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_claim_update_v1",
        language="es_AR",
        category="UTILITY",
        status="approved",
        content_sid="HXtenantowned",
        body_preview="Caso {{1}}: {{2}}.",
        components={
            "button": {
                "type": "URL",
                "url": "https://www.chatboc.ar/reclamos/{{1}}",
            }
        },
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.flush()
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="claim_update",
        channel="whatsapp",
        body_template="Actualización del caso ${claim_code}: ${status}.",
        is_active=True,
        message_template_registry_id=registry.id,
    )
    db.session.add(template)
    db.session.commit()

    preview = preview_notification_template(
        tenant_id=tenant.id,
        template_id=template.id,
        context={"claim_code": "REC-10482", "status": "En tratamiento"},
        content_variables={"1": "REC-10482", "2": "En tratamiento"},
    )

    assert preview["contract_version"] == PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION
    assert preview["tenant_id"] == tenant.id
    assert preview["rendered"]["body"] == (
        "Actualización del caso REC-10482: En tratamiento."
    )
    assert preview["provider_template"]["rendered_body"] == (
        "Caso REC-10482: En tratamiento."
    )
    assert preview["provider_template"]["rendered_components"]["button"]["url"].endswith(
        "/reclamos/REC-10482"
    )
    assert preview["provider_template"]["lifecycle"]["production_send_allowed"] is True
    assert preview["readiness"] == {
        "preview_valid": True,
        "provider_template_approval_valid": True,
        "provider_template_ready": True,
        "transport_readiness_checked": False,
        "production_send_allowed": False,
        "blockers": ["transport_readiness_not_checked"],
    }
    assert preview["side_effects"] == {
        "provider_calls_performed": False,
        "messages_queued": 0,
        "messages_sent": 0,
    }


def test_whatsapp_preview_requires_exact_provider_variables(app_context):
    _, tenant = _seed_tenant("provider-vars")
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_claim_created_v1",
        language="es_AR",
        category="UTILITY",
        status="pending_approval",
        content_sid="HXpending",
        body_preview="Registramos el caso {{1}}. Seguimiento: {{2}}",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.flush()
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="claim_created",
        channel="whatsapp",
        body_template="Registramos ${claim_code}.",
        is_active=True,
        message_template_registry_id=registry.id,
    )
    db.session.add(template)
    db.session.commit()

    with pytest.raises(ProfessionalMessageContractError) as captured:
        preview_notification_template(
            tenant_id=tenant.id,
            key="claim_created",
            channel="whatsapp",
            context={"claim_code": "REC-1"},
            content_variables={"1": "REC-1"},
        )

    assert captured.value.to_dict() == {
        "contract_version": PROFESSIONAL_MESSAGE_PREVIEW_CONTRACT_VERSION,
        "error": "content_variables_missing",
        "field": "content_variables",
        "variable_names": ["2"],
    }


def test_preview_fails_closed_on_cross_tenant_registry_link(app_context):
    _, tenant_a = _seed_tenant("registry-a")
    _, tenant_b = _seed_tenant("registry-b")
    registry_b = MessageTemplateRegistry(
        tenant_id=tenant_b.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_tenant_b_only_v1",
        language="es_AR",
        status="approved",
        content_sid="HXtenantb",
        body_preview="Hola {{1}}",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry_b)
    db.session.flush()
    template_a = NotificationTemplate(
        tenant_id=tenant_a.id,
        key="bad_link",
        channel="whatsapp",
        body_template="Hola ${name}",
        message_template_registry_id=registry_b.id,
    )
    db.session.add(template_a)
    db.session.commit()

    with pytest.raises(ProfessionalMessageContractError) as captured:
        preview_notification_template(
            tenant_id=tenant_a.id,
            template_id=template_a.id,
            context={"name": "Ana"},
            content_variables={"1": "Ana"},
        )

    assert captured.value.code == "notification_template_registry_mismatch"


def test_whatsapp_local_only_preview_is_explicitly_blocked(app_context):
    _, tenant = _seed_tenant("local-only")
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="session_message",
        channel="whatsapp",
        body_template="Hola ${name}",
        is_active=False,
    )
    db.session.add(template)
    db.session.commit()

    preview = preview_notification_template(
        tenant_id=tenant.id,
        template_id=template.id,
        context={"name": "Ana"},
    )

    assert preview["provider_template"] is None
    assert preview["readiness"]["production_send_allowed"] is False
    assert preview["readiness"]["provider_template_ready"] is False
    assert preview["readiness"]["blockers"] == [
        "notification_template_inactive",
        "whatsapp_template_registry_not_linked",
        "transport_readiness_not_checked",
    ]


def test_provider_preview_rejects_malformed_numbered_placeholder(app_context):
    _, tenant = _seed_tenant("malformed-provider")
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_malformed_provider_v1",
        language="es_AR",
        status="approved",
        content_sid="HXmalformed",
        body_preview="Caso {{1 sin cerrar",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.flush()
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="malformed_provider",
        channel="whatsapp",
        body_template="Caso ${claim_code}",
        message_template_registry_id=registry.id,
    )
    db.session.add(template)
    db.session.commit()

    with pytest.raises(ProfessionalMessageContractError) as captured:
        preview_notification_template(
            tenant_id=tenant.id,
            template_id=template.id,
            context={"claim_code": "REC-1"},
        )

    assert captured.value.code == "provider_template_placeholder_invalid"


def test_approved_provider_snapshot_without_content_sid_remains_blocked(app_context):
    _, tenant = _seed_tenant("missing-content-sid")
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="chatboc_external_only_v1",
        language="es_AR",
        status="approved",
        external_template_id="external-provider-reference",
        body_preview="Caso {{1}}",
        last_sync_at=datetime.now(timezone.utc),
    )
    db.session.add(registry)
    db.session.flush()
    template = NotificationTemplate(
        tenant_id=tenant.id,
        key="external_only",
        channel="whatsapp",
        body_template="Caso ${claim_code}",
        message_template_registry_id=registry.id,
    )
    db.session.add(template)
    db.session.commit()

    preview = preview_notification_template(
        tenant_id=tenant.id,
        template_id=template.id,
        context={"claim_code": "REC-1"},
        content_variables={"1": "REC-1"},
    )

    assert preview["provider_template"]["lifecycle"]["production_send_allowed"] is True
    assert preview["readiness"]["provider_template_approval_valid"] is True
    assert preview["readiness"]["provider_template_ready"] is False
    assert preview["readiness"]["production_send_allowed"] is False
    assert preview["readiness"]["blockers"] == [
        "whatsapp_template_content_sid_missing",
        "transport_readiness_not_checked",
    ]
