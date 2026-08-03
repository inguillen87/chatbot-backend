import logging
from types import SimpleNamespace
from unittest.mock import patch

from services import notifications
from services.notification_dispatcher import notification_dispatcher
from services.notification_orchestrator import NotificationOrchestrator
from services.whatsapp_experience import _template_readiness_payload
from models import Notification, NotificationAttempt, TenantProfile, User, db


def test_legacy_template_shim_fails_closed_and_never_logs_pii(app, caplog):
    phone = "+5492613998877"
    name = "Persona Privada Unica"
    ticket_ref = "REC-SECRETO-991"
    category = "categoria-confidencial"
    message = "mensaje medico privado irrepetible"

    with app.app_context(), caplog.at_level(logging.INFO, logger="services.notifications"):
        result = notifications.enviar_notificacion_whatsapp_con_plantilla(
            phone,
            name,
            ticket_ref,
            category,
            message,
        )

    assert result.accepted is False
    assert bool(result) is False
    assert result.reason_code == "whatsapp_tenant_scope_required"
    assert result.provider_message_id is None
    assert result.idempotency_bound is False
    assert notifications.WHATSAPP_TEMPLATE_TRANSPORT_IMPLEMENTED is False
    assert notifications.ORDER_WHATSAPP_TRANSPORT_IMPLEMENTED is False
    for secret in (phone, name, ticket_ref, category, message):
        assert secret not in caplog.text
    assert "has_recipient=True" in caplog.text
    assert f"body_length={len(message)}" in caplog.text


def test_complete_looking_legacy_bindings_still_cannot_fake_provider_acceptance(app):
    with app.app_context():
        result = notifications.enviar_notificacion_whatsapp_con_plantilla(
            "+5492613112233",
            "Vecino",
            "REC-100",
            "reclamo",
            "Confirmacion",
            tenant_id=7,
            template_registry_id=11,
            expected_sender_binding="a" * 64,
            idempotency_key="claim-created:7:100",
        )

    assert result.accepted is False
    assert result.reason_code == "whatsapp_template_transport_unavailable"
    assert result.tenant_id == 7
    assert result.template_registry_id == 11
    assert result.idempotency_bound is True


def test_notification_worker_does_not_emit_synthetic_whatsapp_acceptance(app, client):
    notification = SimpleNamespace(
        id="notif-safe-1",
        tenant_id=98_765,
        channel="whatsapp",
        recipient="+5492613112233",
        body="Respuesta dentro de ventana",
        metadata_json={"within_24h_window": True, "is_template": False},
        attempt_count=0,
    )

    with app.app_context(), patch(
        "services.tenant_twilio_messaging.send_prepared_tenant_twilio_message"
    ) as provider_send:
        accepted, provider_id, error = NotificationOrchestrator._send_stub(notification)

    assert accepted is False
    assert provider_id is None
    assert error == "whatsapp_transport_unavailable"
    provider_send.assert_not_called()


def test_notification_worker_rejects_unbound_template_claim_before_transport(app, client):
    notification = SimpleNamespace(
        id="notif-safe-template-1",
        tenant_id=98_766,
        channel="whatsapp",
        recipient="+5492613112233",
        body="Plantilla declarada por el cliente",
        metadata_json={"within_24h_window": False, "is_template": True},
        attempt_count=0,
    )

    with app.app_context():
        accepted, provider_id, error = NotificationOrchestrator._send_stub(notification)

    assert accepted is False
    assert provider_id is None
    assert error == "whatsapp_template_registry_required"


def test_notification_worker_never_fabricates_email_or_push_acceptance(app):
    with app.app_context():
        for channel in ("email", "push"):
            notification = SimpleNamespace(
                id=f"notif-safe-{channel}",
                tenant_id=98_767,
                channel=channel,
                recipient="recipient@example.test",
                body="Contenido privado",
                metadata_json={},
                attempt_count=0,
            )
            accepted, provider_id, error = NotificationOrchestrator._send_stub(
                notification
            )
            assert accepted is False
            assert provider_id is None
            assert error == f"{channel}_transport_unavailable"


def test_only_in_app_delivery_can_use_a_local_receipt(app):
    notification = SimpleNamespace(
        id="notif-safe-in-app",
        tenant_id=98_768,
        channel="in_app",
        recipient="user:123",
        body="Aviso local",
        metadata_json={},
        attempt_count=0,
    )

    with app.app_context():
        accepted, provider_id, error = NotificationOrchestrator._send_stub(notification)

    assert accepted is True
    assert provider_id == "in_app:notif-safe-in-app:1"
    assert error is None


def test_unavailable_transport_is_blocked_and_tenant_scoped_requeue_is_explicit(
    app, client
):
    with app.app_context():
        owner = User(
            name="Notification Owner A",
            email="notification-owner-a@example.test",
            rol="empresa",
            tipo_chat="pyme",
        )
        owner.set_password("notification-test-password-a")
        other_owner = User(
            name="Notification Owner B",
            email="notification-owner-b@example.test",
            rol="empresa",
            tipo_chat="pyme",
        )
        other_owner.set_password("notification-test-password-b")
        db.session.add_all([owner, other_owner])
        db.session.flush()
        tenant = TenantProfile(
            slug="notification-blocked-a",
            nombre="Notification Blocked A",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
        )
        other_tenant = TenantProfile(
            slug="notification-blocked-b",
            nombre="Notification Blocked B",
            tipo="pyme",
            pyme_id=other_owner.id,
            is_active=True,
        )
        db.session.add_all([tenant, other_tenant])
        db.session.flush()
        owner.tenant_id = tenant.id
        other_owner.tenant_id = other_tenant.id
        notification = Notification(
            tenant_id=tenant.id,
            channel="email",
            recipient="private-a@example.test",
            body="Aviso A",
            status="queued",
            idempotency_key="email-blocked-a",
            max_retries=3,
            attempt_count=0,
            metadata_json={},
        )
        other_notification = Notification(
            tenant_id=other_tenant.id,
            channel="email",
            recipient="private-b@example.test",
            body="Aviso B",
            status="blocked",
            idempotency_key="email-blocked-b",
            max_retries=3,
            attempt_count=1,
            last_error="email_transport_unavailable",
            metadata_json={},
        )
        db.session.add_all([notification, other_notification])
        db.session.commit()

        orchestrator = NotificationOrchestrator(tenant.id)
        summary = orchestrator.dispatch_due_notifications()
        db.session.commit()
        db.session.refresh(notification)
        db.session.refresh(other_notification)

        assert summary == {
            "processed": 1,
            "sent": 0,
            "delayed": 0,
            "blocked": 1,
            "failed": 0,
            "send_uncertain": 0,
        }
        assert notification.status == "blocked"
        assert notification.last_error == "email_transport_unavailable"
        assert notification.next_retry_at is None
        assert NotificationAttempt.query.filter_by(
            notification_id=notification.id,
            status="blocked",
        ).count() == 1

        requeued = orchestrator.requeue_blocked_notifications(
            channels={"email"},
            reason_codes={"email_transport_unavailable"},
        )
        db.session.commit()
        db.session.refresh(notification)
        db.session.refresh(other_notification)

        assert requeued == 1
        assert notification.status == "queued"
        assert notification.last_error is None
        assert notification.next_retry_at is not None
        assert other_notification.status == "blocked"


def test_template_readiness_separates_meta_approval_from_real_transport():
    status = {
        "status": "approved",
        "reference_found": True,
        "content_sid": "HXapprovedbutnotwired",
        "source": "message_template_registry",
    }

    readiness = _template_readiness_payload(status, {})

    assert readiness["template_approved"] is True
    assert readiness["transport_ready"] is False
    assert readiness["production_send_allowed"] is False
    assert readiness["state"] == "approved_transport_unavailable"
    assert (
        readiness["next_action"]
        == "implement_durable_tenant_scoped_template_transport"
    )


def test_order_dispatcher_does_not_log_failed_shim_as_sent(caplog):
    phone = "+5492613445566"
    name = "Cliente PII No Loguear"
    order_ref = "PED-PII-7788"
    pedido = SimpleNamespace(
        tenant_id=77,
        telefono_cliente=phone,
        email_cliente=None,
        nombre_cliente=name,
        nro_pedido=order_ref,
        rubro="Salud privada",
    )
    blocked = notifications.NotificationDispatchResult(
        channel="whatsapp",
        accepted=False,
        reason_code="whatsapp_tenant_scope_required",
    )

    with patch(
        "services.notification_dispatcher.enviar_notificacion_whatsapp_con_plantilla",
        return_value=blocked,
    ), caplog.at_level(logging.INFO, logger="services.notification_dispatcher"):
        notification_dispatcher._notify_customer(pedido, None)

    assert "Customer WhatsApp sent" not in caplog.text
    assert "Customer WhatsApp blocked tenant_id=77" in caplog.text
    for secret in (phone, name, order_ref):
        assert secret not in caplog.text
