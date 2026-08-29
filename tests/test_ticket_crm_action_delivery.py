from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qsl, urlparse

import pytest

from models import (
    AuditEvent,
    DomainEffectOutbox,
    MessageTemplateRegistry,
    MunicipioTicket,
    Notification,
    NotificationAttempt,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    User,
    WhatsAppContactState,
    db,
)
from services.domain_effect_outbox import dispatch_domain_effects
from services.notification_orchestrator import reconcile_whatsapp_notification_status
from services import ticket_crm_action_delivery as crm_action_delivery
from services.ticket_crm_action_delivery import (
    CrmActionDeliveryError,
    create_ticket_crm_action_audit_event,
    stage_legacy_ticket_crm_action_delivery,
)
from services.ticket_domain_effects import TICKET_DOMAIN_EFFECT_REGISTRY
from services.tenant_twilio_messaging import TenantTwilioMessagePreflight


SECRET = "crm-action-domain-effect-secret-at-least-32-bytes"
PHONE = "+5492613168608"


def _enable_outbox(app, monkeypatch, tenant_id: int) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", SECRET)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(tenant_id))
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", 4096)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", 8)


def _seed_scope(app, monkeypatch, owner_user):
    tenant = TenantProfile(
        slug="crm-action-junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    owner_user.tenant_id = tenant.id
    owner_user.tenant_slug = tenant.slug

    employee = User(
        name="Operador Luminarias",
        email="operador-luminarias@test.com",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        accesibilidad={"employee_scope": {"categorias": ["luminarias"]}},
    )
    employee.set_password("secret123")
    db.session.add(employee)
    db.session.flush()

    account_sid = f"AC{tenant.id:032x}"
    token_ref = "CRM_ACTION_TWILIO_TOKEN"
    monkeypatch.setitem(app.config, token_ref, "tenant-scoped-test-token")
    tenant.configuracion = {
        "twilio_tech_provider": {
            "twilio_account_sid": account_sid,
            "twilio_subaccount_token_ref": token_ref,
        }
    }
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=account_sid,
        credentials_ref=f"env:{token_ref}",
        capabilities={"session_location_messages": True},
    )
    db.session.add(connection)
    db.session.flush()
    sender_phone = f"+1500555{tenant.id:04d}"
    sender = ProviderSender(
        tenant_id=tenant.id,
        provider_connection_id=connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number=sender_phone,
        sender_id=f"whatsapp:{sender_phone}",
        status="online",
        status_callback_url="https://api.example.test/twilio/whatsapp/status",
    )
    db.session.add(sender)
    db.session.flush()

    ticket = MunicipioTicket(
        pregunta="Luminaria apagada",
        asunto="Luminaria",
        categoria="Luminarias",
        municipio_id=owner_user.id,
        tenant_id=tenant.id,
        asignado_a_id=employee.id,
        estado="en_proceso",
        canal_ingreso="whatsapp",
        telefono_vecino=PHONE,
        datos_extra={},
    )
    db.session.add(ticket)
    db.session.flush()
    _enable_outbox(app, monkeypatch, tenant.id)
    return tenant, employee, connection, sender, ticket


def _record_action(
    ticket: MunicipioTicket,
    actor: User,
    *,
    action: str,
    payload: dict,
) -> TicketComentario:
    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario=f"Acción CRM: {action}",
        user_id=actor.id,
        es_admin=True,
        origen="internal",
    )
    db.session.add(comment)
    db.session.flush()
    ticket.datos_extra = {
        "crm_reply_actions": [
            {
                "contract_version": "inbox.crm_reply_action.v1",
                "comment_id": comment.id,
                "action": action,
                "payload": dict(payload),
                "delivery_mode": "internal_event",
                "external_dispatch": False,
            }
        ]
    }
    db.session.add(ticket)
    db.session.flush()
    audit_event = create_ticket_crm_action_audit_event(
        ticket=ticket,
        comment=comment,
        tenant=db.session.get(TenantProfile, ticket.tenant_id),
        actor=actor,
        action=action,
        action_payload=payload,
    )
    comment.crm_action_audit_event = audit_event
    return comment


def _mock_published_form(monkeypatch, payload: dict) -> dict:
    canonical = {
        "id": int(payload.get("id") or 9001),
        "form_slug": str(payload["form_slug"]).strip().lower(),
        "label": str(payload.get("label") or "Formulario").strip(),
        "href": str(
            payload.get("published_href")
            or f"https://www.chatboc.ar/e/{payload['form_slug']}"
        ),
        "kind": str(payload.get("kind") or "encuesta").strip().lower(),
    }

    def resolver(*, tenant_id, form_slug):
        assert int(tenant_id) > 0
        return dict(canonical) if str(form_slug).strip().lower() == canonical["form_slug"] else None

    monkeypatch.setattr(
        "services.ticket_crm_action_delivery.resolve_tenant_public_form_action_payload",
        resolver,
    )
    return canonical


def _open_session(tenant: TenantProfile, *, at=None) -> None:
    db.session.add(
        WhatsAppContactState(
            tenant_id=tenant.id,
            recipient=PHONE,
            last_inbound_at=at or datetime.now(timezone.utc),
        )
    )
    db.session.flush()


def _approved_form_template(
    tenant: TenantProfile,
    *,
    form_slug: str,
) -> MessageTemplateRegistry:
    row = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"crm-form-{form_slug}",
        language="es",
        category="UTILITY",
        status="approved",
        content_sid="HX" + ("a" * 32),
        last_sync_at=datetime.now(timezone.utc),
        metadata_json={
            "crm_form_delivery": {
                "enabled": True,
                "form_slug": form_slug,
                "variable_sources": {
                    "1": "form_url",
                    "2": "ticket_reference",
                },
            }
        },
    )
    db.session.add(row)
    db.session.flush()
    return row


def _stage_location_effect(
    *,
    tenant: TenantProfile,
    employee: User,
    ticket: MunicipioTicket,
) -> tuple[Notification, NotificationAttempt]:
    payload = {"lat": -34.5892, "lng": -60.9467, "label": "Plaza principal"}
    comment = _record_action(
        ticket,
        employee,
        action="share_location",
        payload=payload,
    )
    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    assert decision.durably_staged is True
    db.session.commit()
    return (
        Notification.query.filter_by(tenant_id=tenant.id).one(),
        NotificationAttempt.query.filter_by(tenant_id=tenant.id).one(),
    )


def _assert_terminal_callback_state(
    *,
    notification_id: str,
    attempt_id: str,
    notification_status: str,
    attempt_status: str,
    error_code: str,
) -> tuple[Notification, NotificationAttempt]:
    db.session.expire_all()
    notification = db.session.get(Notification, notification_id)
    attempt = db.session.get(NotificationAttempt, attempt_id)
    assert notification is not None
    assert attempt is not None
    assert notification.status == notification_status
    assert attempt.status == attempt_status
    assert notification.attempt_count == 1
    assert notification.lease_token is None
    assert notification.leased_until is None
    assert notification.next_retry_at is None
    assert attempt.next_retry_at is None
    assert notification.last_error == error_code
    assert attempt.error_message == error_code
    assert attempt.error_digest is not None
    assert len(attempt.error_digest) == 64
    return notification, attempt


def test_location_is_staged_and_worker_builds_geo_persistent_action(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {
        "lat": -34.5892,
        "lng": -60.9467,
        "label": "Plaza principal",
        "address": "Junín, Buenos Aires",
    }
    comment = _record_action(ticket, employee, action="share_location", payload=payload)

    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    db.session.commit()

    assert decision.durably_staged is True
    assert decision.to_dict()["external_dispatch"] is False
    row = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).one()
    serialized = str(row.payload_json)
    assert PHONE not in serialized
    assert "-34.5892" not in serialized
    assert "Plaza principal" not in serialized
    notification = Notification.query.filter_by(tenant_id=tenant.id).one()
    attempt = NotificationAttempt.query.filter_by(tenant_id=tenant.id).one()
    assert notification.status == Notification.STATUS_BLOCKED
    assert attempt.status == NotificationAttempt.STATUS_BLOCKED
    assert attempt.notification_id == notification.id

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message",
        return_value="SM-location-accepted",
    ) as sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.succeeded == 1
    prepared = sender.call_args.args[0]
    assert "body" not in prepared.params
    assert prepared.params["persistent_action"] == [
        "geo:-34.5892000,-60.9467000|Plaza principal"
    ]
    assert prepared.params["to"] == f"whatsapp:{PHONE}"
    callback_query = dict(parse_qsl(urlparse(prepared.params["status_callback"]).query))
    assert callback_query["notification_attempt_id"] == attempt.id
    db.session.expire_all()
    assert db.session.get(Notification, notification.id).status == Notification.STATUS_SENT
    assert (
        db.session.get(NotificationAttempt, attempt.id).status
        == NotificationAttempt.STATUS_SUCCESS
    )
    assert reconcile_whatsapp_notification_status(
        tenant_id=tenant.id,
        notification_attempt_id=attempt.id,
        provider_message_sid="SM-location-accepted",
        provider_status="delivered",
        provider_sender_id=notification.provider_sender_id,
    ) is True
    db.session.expire_all()
    correlated = db.session.get(Notification, notification.id)
    assert correlated.provider_status == "delivered"
    assert correlated.metadata_json["audit_event_id"] == comment.crm_action_audit_event.id


def test_correlated_preflight_failure_after_sending_closes_failed_without_dispatch(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    notification, attempt = _stage_location_effect(
        tenant=tenant,
        employee=employee,
        ticket=ticket,
    )
    original_preflight = crm_action_delivery.prepare_bound_tenant_twilio_message

    def fail_only_correlated_preflight(**kwargs):
        if kwargs.get("notification_attempt_id"):
            return TenantTwilioMessagePreflight(
                reason_code="whatsapp_status_callback_missing"
            )
        return original_preflight(**kwargs)

    monkeypatch.setattr(
        crm_action_delivery,
        "prepare_bound_tenant_twilio_message",
        fail_only_correlated_preflight,
    )
    with patch.object(
        crm_action_delivery,
        "send_prepared_tenant_twilio_message",
    ) as provider_send:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.unknown == 1
    provider_send.assert_not_called()
    terminal_notification, terminal_attempt = _assert_terminal_callback_state(
        notification_id=notification.id,
        attempt_id=attempt.id,
        notification_status=Notification.STATUS_FAILED,
        attempt_status=NotificationAttempt.STATUS_FAILED,
        error_code="whatsapp_status_callback_missing",
    )
    assert terminal_notification.provider_status == Notification.PROVIDER_STATUS_UNKNOWN
    assert terminal_attempt.provider_status == Notification.PROVIDER_STATUS_UNKNOWN
    assert terminal_attempt.metadata_json.get("provider_call_started") is not True
    outbox = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).one()
    assert outbox.status == DomainEffectOutbox.STATUS_UNKNOWN
    assert outbox.lease_token is None
    assert outbox.leased_until is None


def test_provider_failure_before_io_hook_closes_failed_without_retry(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    notification, attempt = _stage_location_effect(
        tenant=tenant,
        employee=employee,
        ticket=ticket,
    )

    with patch.object(
        crm_action_delivery,
        "send_prepared_tenant_twilio_message",
        side_effect=RuntimeError("provider secret must not persist"),
    ) as provider_send:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.unknown == 1
    provider_send.assert_called_once()
    terminal_notification, terminal_attempt = _assert_terminal_callback_state(
        notification_id=notification.id,
        attempt_id=attempt.id,
        notification_status=Notification.STATUS_FAILED,
        attempt_status=NotificationAttempt.STATUS_FAILED,
        error_code="crm_action_provider_call_failed_before_io",
    )
    assert "secret" not in str(terminal_notification.last_error).lower()
    assert "secret" not in str(terminal_attempt.error_message).lower()
    assert terminal_attempt.metadata_json.get("provider_call_started") is not True
    second = dispatch_domain_effects(
        registry=TICKET_DOMAIN_EFFECT_REGISTRY,
        intent_secret=SECRET,
        tenant_id=tenant.id,
    )
    assert second.processed == 0
    provider_send.assert_called_once()


@pytest.mark.parametrize("provider_outcome", ["raise", "empty"])
def test_provider_outcome_after_io_hook_closes_send_uncertain_without_redispatch(
    app,
    monkeypatch,
    init_database,
    owner_user,
    provider_outcome,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    notification, attempt = _stage_location_effect(
        tenant=tenant,
        employee=employee,
        ticket=ticket,
    )

    def ambiguous_provider(_prepared, *, on_provider_call_start=None):
        assert on_provider_call_start is not None
        on_provider_call_start()
        if provider_outcome == "raise":
            raise RuntimeError("provider outcome intentionally ambiguous")
        return None

    with patch.object(
        crm_action_delivery,
        "send_prepared_tenant_twilio_message",
        side_effect=ambiguous_provider,
    ) as provider_send:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )
        second = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.unknown == 1
    assert second.processed == 0
    provider_send.assert_called_once()
    terminal_notification, terminal_attempt = _assert_terminal_callback_state(
        notification_id=notification.id,
        attempt_id=attempt.id,
        notification_status=Notification.STATUS_SEND_UNCERTAIN,
        attempt_status=NotificationAttempt.STATUS_SEND_UNCERTAIN,
        error_code="provider_acceptance_unknown",
    )
    assert terminal_notification.provider_status == Notification.PROVIDER_STATUS_UNKNOWN
    assert terminal_attempt.provider_status == Notification.PROVIDER_STATUS_UNKNOWN
    assert terminal_attempt.metadata_json["provider_call_started"] is True


def test_provider_sid_then_local_acceptance_failure_keeps_reference_and_closes_uncertain(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    notification, attempt = _stage_location_effect(
        tenant=tenant,
        employee=employee,
        ticket=ticket,
    )
    provider_sid = "SM-accepted-before-local-persistence-failure"

    def accepted_provider(_prepared, *, on_provider_call_start=None):
        assert on_provider_call_start is not None
        on_provider_call_start()
        return provider_sid

    with (
        patch.object(
            crm_action_delivery,
            "send_prepared_tenant_twilio_message",
            side_effect=accepted_provider,
        ) as provider_send,
        patch.object(
            crm_action_delivery,
            "_record_callback_provider_acceptance",
            side_effect=RuntimeError("local acceptance write failed"),
        ),
    ):
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.unknown == 1
    provider_send.assert_called_once()
    terminal_notification, terminal_attempt = _assert_terminal_callback_state(
        notification_id=notification.id,
        attempt_id=attempt.id,
        notification_status=Notification.STATUS_SEND_UNCERTAIN,
        attempt_status=NotificationAttempt.STATUS_SEND_UNCERTAIN,
        error_code="crm_action_provider_acceptance_persistence_unknown",
    )
    assert terminal_notification.provider_message_id == provider_sid
    assert terminal_attempt.provider_message_id == provider_sid
    assert terminal_notification.provider_status == "accepted"
    assert terminal_attempt.provider_status == "accepted"
    assert terminal_attempt.metadata_json["provider_call_started"] is True


@pytest.mark.parametrize(
    ("session_age", "capability", "reason"),
    [
        (timedelta(hours=25), True, "whatsapp_session_window_closed"),
        (timedelta(minutes=5), False, "whatsapp_location_capability_missing"),
    ],
)
def test_location_falls_back_to_internal_crm_without_false_delivery(
    app,
    monkeypatch,
    init_database,
    owner_user,
    session_age,
    capability,
    reason,
):
    tenant, employee, connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    connection.capabilities = {"session_location_messages": capability}
    _open_session(tenant, at=datetime.now(timezone.utc) - session_age)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)

    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )

    assert decision.delivery_mode == "internal_event"
    assert decision.reason_code == reason
    assert decision.to_dict()["final_delivery"]["status"] == "not_dispatched"
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_form_requires_fresh_approved_tenant_content_and_dispatches_template(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    template = _approved_form_template(tenant, form_slug="luminarias-seguimiento")
    published = _mock_published_form(
        monkeypatch,
        {
            "id": 632,
            "form_slug": "luminarias-seguimiento",
            "label": "Seguimiento de luminarias",
            "published_href": "https://www.chatboc.ar/e/luminarias-seguimiento",
            "kind": "encuesta",
        },
    )
    payload = {
        "form_slug": "luminarias-seguimiento",
        "label": "Etiqueta controlada por caller",
        "href": "https://attacker.example/formulario",
    }
    comment = _record_action(ticket, employee, action="share_form", payload=payload)
    assert comment.crm_action_audit_event.details["action_payload"] == published

    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    db.session.commit()
    assert decision.durably_staged is True

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message",
        return_value="SM-form-accepted",
    ) as sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.succeeded == 1
    params = sender.call_args.args[0].params
    assert params["content_sid"] == template.content_sid
    assert "body" not in params
    assert "persistent_action" not in params
    assert "luminarias-seguimiento" in params["content_variables"]


def test_form_without_explicit_approved_mapping_remains_internal_only(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    payload = {
        "form_slug": "sin-template",
        "label": "Formulario sin template",
        "href": "https://www.chatboc.ar/e/sin-template",
    }
    _mock_published_form(monkeypatch, payload)
    comment = _record_action(ticket, employee, action="share_form", payload=payload)

    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )

    assert decision.delivery_mode == "internal_event"
    assert decision.reason_code == "whatsapp_form_content_not_approved"
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_employee_must_own_ticket_before_staging_external_action(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)
    ticket.asignado_a_id = None
    db.session.flush()

    with pytest.raises(CrmActionDeliveryError) as raised:
        stage_legacy_ticket_crm_action_delivery(
            ticket=ticket,
            comment=comment,
            tenant=tenant,
            actor=employee,
            audit_event=comment.crm_action_audit_event,
        )

    assert raised.value.code == "crm_action_employee_assignment_required"
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_cross_tenant_action_is_rejected_before_outbox_staging(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    foreign = TenantProfile(
        slug="crm-action-foreign",
        nombre="Municipio ajeno",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(foreign)
    db.session.flush()
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)

    with pytest.raises(CrmActionDeliveryError) as raised:
        stage_legacy_ticket_crm_action_delivery(
            ticket=ticket,
            comment=comment,
            tenant=foreign,
            actor=employee,
            audit_event=comment.crm_action_audit_event,
        )

    assert raised.value.code == "crm_action_ticket_tenant_mismatch"
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=foreign.id).count() == 0


def test_recipient_change_after_staging_is_dead_before_provider_io(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)
    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    assert decision.durably_staged
    ticket.telefono_vecino = "+5492613000000"
    db.session.commit()

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.dead == 1
    sender.assert_not_called()
    row = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).one()
    assert row.last_error_code == "crm_action_recipient_binding_invalid"


def test_staging_same_persisted_action_is_idempotent(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)

    first = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    second = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.effect_id == second.effect_id
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 1


def test_worker_uses_audit_event_after_bounded_ui_history_is_pruned(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)
    decision = stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=comment.crm_action_audit_event,
    )
    assert decision.durably_staged
    ticket.datos_extra = {
        "crm_reply_actions": [
            {
                "contract_version": "inbox.crm_reply_action.v1",
                "comment_id": index + 10_000,
                "action": "share_location",
                "payload": {"lat": 0, "lng": 0, "label": f"UI {index}"},
            }
            for index in range(100)
        ]
    }
    db.session.commit()

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message",
        return_value="SM-audit-source",
    ) as sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.succeeded == 1
    sender.assert_called_once()


def test_missing_authoritative_audit_event_is_dead_before_provider_io(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    _open_session(tenant)
    payload = {"lat": -34.58, "lng": -60.94, "label": "Centro"}
    comment = _record_action(ticket, employee, action="share_location", payload=payload)
    audit_event = comment.crm_action_audit_event
    stage_legacy_ticket_crm_action_delivery(
        ticket=ticket,
        comment=comment,
        tenant=tenant,
        actor=employee,
        audit_event=audit_event,
    )
    db.session.delete(audit_event)
    db.session.commit()

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
        )

    assert summary.dead == 1
    sender.assert_not_called()
    outbox = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).one()
    assert outbox.last_error_code == "crm_action_audit_event_missing"


def test_unallowlisted_superadmin_role_is_rejected_fail_closed(
    app,
    monkeypatch,
    init_database,
    owner_user,
):
    tenant, _employee, _connection, _sender, ticket = _seed_scope(
        app, monkeypatch, owner_user
    )
    actor = User(
        name="Rol adulterado",
        email="not-allowlisted-superadmin@test.com",
        rol="superadmin",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    actor.set_password("secret123")
    db.session.add(actor)
    db.session.flush()
    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario="Acción CRM: share_location",
        user_id=actor.id,
        es_admin=True,
        origen="internal",
    )
    db.session.add(comment)
    db.session.flush()
    monkeypatch.setattr(
        "services.ticket_crm_action_delivery.is_authorized_superadmin_user",
        lambda _actor: False,
    )

    with pytest.raises(CrmActionDeliveryError) as raised:
        create_ticket_crm_action_audit_event(
            ticket=ticket,
            comment=comment,
            tenant=tenant,
            actor=actor,
            action="share_location",
            action_payload={"lat": -34.58, "lng": -60.94, "label": "Centro"},
        )

    assert raised.value.code == "crm_action_superadmin_not_authorized"
    assert AuditEvent.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
