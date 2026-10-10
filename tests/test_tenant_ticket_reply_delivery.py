from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from pathlib import Path

import pytest

from models import (
    AuditEvent,
    MessageTemplateRegistry,
    MessagingEventLedger,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    TenantTicket,
    TenantTicketReplyEvent,
    WhatsAppContactState,
    db,
)
from services.tenant_ticket_reply_delivery import (
    TenantTicketReplyDeliveryError,
    TenantTicketReplyProviderMessageCollision,
    prepare_whatsapp_reply_policy,
    render_whatsapp_template_body_snapshot,
    reconcile_provider_callback,
    record_provider_acceptance,
    whatsapp_service_window,
)


def _tenant(owner_user, *, slug: str) -> TenantProfile:
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Municipio {slug}",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _sender(tenant: TenantProfile, *, suffix: str = "1") -> ProviderSender:
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id="AC" + (suffix * 32)[:32],
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=f"+1743264371{suffix[-1]}",
        sender_id=f"whatsapp:+1743264371{suffix[-1]}",
        status="online",
    )
    db.session.add(sender)
    db.session.flush()
    return sender


def test_whatsapp_free_form_requires_open_24h_window_or_approved_template(
    app, init_database, owner_user
):
    tenant = _tenant(owner_user, slug="reply-window-policy")
    sender = _sender(tenant)
    recipient = "+5492613168608"
    now = datetime.now(timezone.utc)
    contact = WhatsAppContactState(
        tenant_id=tenant.id,
        provider_sender_id=sender.id,
        recipient=f"whatsapp:{recipient}",
        last_inbound_at=now - timedelta(hours=23),
    )
    db.session.add(contact)
    db.session.commit()

    template_id, variables, open_snapshot = prepare_whatsapp_reply_policy(
        tenant_id=tenant.id,
        provider_sender_id=sender.id,
        recipient=recipient,
        now=now,
    )
    assert template_id is None
    assert variables is None
    assert open_snapshot["status"] == "open"
    assert open_snapshot["decision"] == "free_form"

    contact.last_inbound_at = now - timedelta(hours=25)
    template = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name="municipio_seguimiento_aprobado",
        language="es_AR",
        category="UTILITY",
        status="approved",
        content_sid="HX" + ("a" * 32),
        last_sync_at=now,
        body_preview="Actualizamos el reclamo {{1}}: {{2}}.",
    )
    db.session.add(template)
    db.session.commit()

    closed = whatsapp_service_window(
        tenant_id=tenant.id,
        provider_sender_id=sender.id,
        recipient=recipient,
        now=now,
    )
    assert closed["status"] == "expired"
    assert closed["template_required"] is True
    with pytest.raises(TenantTicketReplyDeliveryError) as error:
        prepare_whatsapp_reply_policy(
            tenant_id=tenant.id,
            provider_sender_id=sender.id,
            recipient=recipient,
            now=now,
        )
    assert error.value.code == "whatsapp_template_required_outside_24h"

    selected_id, selected_variables, snapshot = prepare_whatsapp_reply_policy(
        tenant_id=tenant.id,
        provider_sender_id=sender.id,
        recipient=recipient,
        template_registry_id=template.id,
        template_variables={"1": "M-419", "2": "cuadrilla asignada"},
        now=now,
    )
    assert selected_id == template.id
    assert selected_variables == {"1": "M-419", "2": "cuadrilla asignada"}
    assert snapshot["decision"] == "approved_template"
    assert (
        snapshot["_delivery_binding"]["delivery_body_snapshot"]
        == "Actualizamos el reclamo M-419: cuadrilla asignada."
    )
    assert (
        snapshot["_delivery_binding"]["delivery_body_source"]
        == "approved_template_registry_snapshot"
    )


def test_template_body_snapshot_rejects_missing_or_unresolved_content():
    assert render_whatsapp_template_body_snapshot(
        "Reclamo {{1}}: {{2}}.",
        {"1": "M-419", "2": "cuadrilla asignada"},
    ) == "Reclamo M-419: cuadrilla asignada."

    with pytest.raises(TenantTicketReplyDeliveryError) as missing:
        render_whatsapp_template_body_snapshot("", {})
    assert missing.value.code == "whatsapp_template_body_preview_missing"

    with pytest.raises(TenantTicketReplyDeliveryError) as unresolved:
        render_whatsapp_template_body_snapshot("Hola {{nombre}}", {})
    assert unresolved.value.code == "whatsapp_template_body_unresolved"


def test_whatsapp_service_window_isolated_by_sender(
    app, init_database, owner_user
):
    tenant = _tenant(owner_user, slug="reply-window-sender-isolation")
    sender_a = _sender(tenant, suffix="2")
    sender_b = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=sender_a.provider_connection_id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number="+17432643713",
        sender_id="whatsapp:+17432643713",
        status="online",
    )
    db.session.add(sender_b)
    db.session.flush()
    recipient = "+5492613168608"
    now = datetime.now(timezone.utc)
    db.session.add(
        WhatsAppContactState(
            tenant_id=tenant.id,
            provider_sender_id=sender_a.id,
            recipient=recipient,
            last_inbound_at=now - timedelta(minutes=5),
        )
    )
    db.session.commit()

    open_for_a = whatsapp_service_window(
        tenant_id=tenant.id,
        provider_sender_id=sender_a.id,
        recipient=recipient,
        now=now,
    )
    closed_for_b = whatsapp_service_window(
        tenant_id=tenant.id,
        provider_sender_id=sender_b.id,
        recipient=recipient,
        now=now,
    )

    assert open_for_a["status"] == "open"
    assert closed_for_b["status"] == "unknown"
    assert closed_for_b["free_form_allowed"] is False


def test_provider_callback_is_monotonic_idempotent_and_audited_without_pii(
    app, init_database, owner_user
):
    tenant = _tenant(owner_user, slug="reply-provider-callback")
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id="AC" + ("b" * 32),
    )
    db.session.add(connection)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number="+17432643718",
        sender_id="whatsapp:+17432643718",
        status="online",
    )
    db.session.add(sender)
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=owner_user.id,
        categoria="luminarias",
        descripcion="Luminaria apagada",
        estado="en_proceso",
        origen="whatsapp",
    )
    db.session.add(ticket)
    db.session.flush()
    provider_message_id = "SM" + ("c" * 32)
    private_body = "La cuadrilla recibió el reclamo del vecino privado."
    private_phone = "+5492613168608"
    reply = TenantTicketReplyEvent(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        event_id="reply-callback-0001",
        body=private_body,
        recipient_phone=private_phone,
        whatsapp_delivery_status="uncertain",
        whatsapp_provider_status="unknown",
        whatsapp_error_code="provider_acceptance_unknown",
    )
    db.session.add(reply)
    db.session.flush()

    def ledger(status: str) -> MessagingEventLedger:
        event = MessagingEventLedger(
            tenant_id=tenant.id,
            provider_connection_id=connection.id,
            provider_sender_id=sender.id,
            channel="whatsapp",
            direction="outbound",
            event_type="status_callback",
            provider="twilio",
            provider_event_id=f"{provider_message_id}:{status}",
            external_message_sid=provider_message_id,
            external_status=status,
        )
        db.session.add(event)
        db.session.flush()
        return event

    delivered_event = ledger("delivered")
    db.session.commit()
    delivered = reconcile_provider_callback(
        tenant_id=tenant.id,
        reply_event_record_id=reply.id,
        provider_message_id=provider_message_id,
        provider_status="delivered",
        provider_sender_id=sender.id,
        delivery_event_id=delivered_event.id,
    )
    assert delivered is not None
    assert delivered.whatsapp_delivery_status == "delivered"
    delivered_updated_at = delivered.whatsapp_status_updated_at

    replayed = reconcile_provider_callback(
        tenant_id=tenant.id,
        reply_event_record_id=reply.id,
        provider_message_id=provider_message_id,
        provider_status="delivered",
        provider_sender_id=sender.id,
        delivery_event_id=delivered_event.id,
    )
    assert replayed is not None
    assert replayed.whatsapp_status_updated_at == delivered_updated_at

    read_event = ledger("read")
    db.session.commit()
    read = reconcile_provider_callback(
        tenant_id=tenant.id,
        reply_event_record_id=reply.id,
        provider_message_id=provider_message_id,
        provider_status="read",
        provider_sender_id=sender.id,
        delivery_event_id=read_event.id,
    )
    assert read is not None
    assert read.whatsapp_delivery_status == "read"
    read_status_event_id = read.whatsapp_status_event_id
    record_provider_acceptance(
        reply_event_record_id=reply.id,
        tenant_id=tenant.id,
        provider_message_id=provider_message_id,
        provider_sender_id=sender.id,
    )
    db.session.expire_all()
    assert db.session.get(TenantTicketReplyEvent, reply.id).whatsapp_delivery_status == "read"

    failed_event = ledger("failed")
    db.session.commit()
    late_failure = reconcile_provider_callback(
        tenant_id=tenant.id,
        reply_event_record_id=reply.id,
        provider_message_id=provider_message_id,
        provider_status="failed",
        provider_sender_id=sender.id,
        delivery_event_id=failed_event.id,
        error_code="30007",
    )
    assert late_failure is not None
    assert late_failure.whatsapp_delivery_status == "read"
    assert late_failure.whatsapp_status_event_id == read_status_event_id

    audits = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="tenant_ticket.reply.whatsapp.delivery_callback",
    ).order_by(AuditEvent.id.asc()).all()
    assert len(audits) == 3
    serialized = str([audit.details for audit in audits])
    assert provider_message_id not in serialized
    assert private_phone not in serialized
    assert private_body not in serialized
    assert "provider_message_id_sha256" in audits[-1].details
    assert audits[-1].details["applied"] is False


def test_provider_message_collision_is_quarantined_without_automatic_retry(
    app, init_database, owner_user
):
    tenant = _tenant(owner_user, slug="reply-provider-collision")
    sender = _sender(tenant, suffix="7")
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=owner_user.id,
        categoria="luminarias",
        descripcion="Dos respuestas independientes",
        estado="en_proceso",
        origen="whatsapp",
    )
    db.session.add(ticket)
    db.session.flush()
    first = TenantTicketReplyEvent(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        event_id="reply-collision-owner-0001",
        body="Primera respuesta",
        recipient_phone="+5492613168608",
        whatsapp_delivery_status="queued",
    )
    collided = TenantTicketReplyEvent(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        event_id="reply-collision-quarantine-0002",
        body="Segunda respuesta distinta",
        recipient_phone="+5492613168608",
        whatsapp_delivery_status="queued",
    )
    db.session.add_all([first, collided])
    db.session.commit()
    provider_message_id = "SM" + ("d" * 32)

    record_provider_acceptance(
        reply_event_record_id=first.id,
        tenant_id=tenant.id,
        provider_message_id=provider_message_id,
        provider_sender_id=sender.id,
    )
    with pytest.raises(TenantTicketReplyProviderMessageCollision) as error:
        record_provider_acceptance(
            reply_event_record_id=collided.id,
            tenant_id=tenant.id,
            provider_message_id=provider_message_id,
            provider_sender_id=sender.id,
        )
    assert error.value.code == "whatsapp_provider_message_id_duplicate"

    db.session.expire_all()
    owner = db.session.get(TenantTicketReplyEvent, first.id)
    quarantined = db.session.get(TenantTicketReplyEvent, collided.id)
    assert owner.whatsapp_delivery_status == "provider_accepted"
    assert owner.whatsapp_provider_message_id == provider_message_id
    assert quarantined.whatsapp_delivery_status == "uncertain"
    assert quarantined.whatsapp_provider_message_id is None
    assert quarantined.whatsapp_provider_status == "collision_quarantined"
    assert (
        quarantined.whatsapp_error_code
        == "whatsapp_provider_message_id_duplicate"
    )

    audit = AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type="tenant_ticket.reply.whatsapp.provider_message_collision",
        resource_id=str(collided.id),
    ).one()
    serialized = str(audit.details)
    assert provider_message_id not in serialized
    assert "Primera respuesta" not in serialized
    assert "+5492613168608" not in serialized
    assert audit.details["automatic_retry_allowed"] is False
    assert audit.details["quarantined"] is True
    assert audit.details["provider_message_id_sha256"] == hashlib.sha256(
        provider_message_id.encode("utf-8")
    ).hexdigest()


def test_delivery_migration_targets_current_head_and_is_forward_only():
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "migrations"
        / "versions"
        / "20260904_add_tenant_ticket_reply_delivery.py"
    )
    spec = importlib.util.spec_from_file_location("tenant_reply_delivery_migration", migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "20260831_inbox_artifact_v1"
    with pytest.raises(RuntimeError, match="forward-only.*audit evidence"):
        migration.downgrade()
