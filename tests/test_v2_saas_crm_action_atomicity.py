from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest

from models import (
    AuditEvent,
    DomainEffectOutbox,
    MunicipioTicket,
    Notification,
    NotificationAttempt,
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    TicketComentario,
    TicketDomainEffectReceipt,
    User,
    WhatsAppContactState,
    db,
)
from services.ticket_crm_action_delivery import (
    ACTION_AUDIT_EVENT_TYPE,
    ACTION_AUDIT_RESOURCE_TYPE,
)


OUTBOX_SECRET = "endpoint-crm-action-secret-at-least-32-bytes"
REQUESTER_PHONE = "+5492613168608"


def _auth_headers(app, user: User, tenant: TenantProfile, idempotency_key: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant.slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant.slug,
        "Idempotency-Key": idempotency_key,
    }


def _location_payload(ticket_id: int) -> dict:
    return {
        "source_model": "MunicipioTicket",
        "legacy_id": ticket_id,
        "action": "share_location",
        "location": {
            "lat": -34.58333,
            "lng": -60.94361,
            "address": "Plaza 25 de Mayo, Junín",
            "label": "Punto de atención municipal",
        },
    }


@pytest.fixture
def municipal_crm_scope(client, init_database, owner_user):
    tenant = TenantProfile(
        slug="endpoint-crm-action-junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.flush()

    owner_user.tenant_id = tenant.id
    owner_user.tenant_slug = tenant.slug
    owner_user.tipo_chat = "municipio"

    employee = User(
        name="Operador de Luminarias",
        email="endpoint-luminarias@test.com",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        tipo_chat="municipio",
        accesibilidad={
            "employee_scope": {
                "categorias": ["luminarias"],
                "channels": ["whatsapp"],
            }
        },
    )
    employee.set_password("secret123")
    db.session.add(employee)
    db.session.flush()

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner_user.id,
        nro_ticket="M-ENDPOINT-CRM-ACTION",
        consulta_pin="CRM001",
        pregunta="Luminaria apagada",
        asunto="Alumbrado público",
        categoria="luminarias",
        detalles="La luminaria de la plaza no enciende",
        estado="en_proceso",
        asignado_a_id=employee.id,
        canal_ingreso="whatsapp",
        nombre_vecino="Vecino de prueba",
        telefono_vecino=REQUESTER_PHONE,
        datos_extra={},
    )
    db.session.add(ticket)
    db.session.commit()
    return SimpleNamespace(
        tenant=tenant,
        owner=owner_user,
        employee=employee,
        ticket=ticket,
    )


def _enable_internal_only(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")


def _enable_durable_location_delivery(app, monkeypatch, scope) -> None:
    tenant = scope.tenant
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", OUTBOX_SECRET)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(tenant.id))
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", 4096)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", 8)

    account_sid = f"AC{tenant.id:032x}"
    token_ref = "ENDPOINT_CRM_ACTION_TWILIO_TOKEN"
    monkeypatch.setitem(app.config, token_ref, "tenant-scoped-test-token")
    tenant.configuracion = {
        "twilio_tech_provider": {
            "twilio_account_sid": account_sid,
            "twilio_subaccount_token_ref": token_ref,
        }
    }
    db.session.add(tenant)

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
    db.session.add(
        ProviderSender(
            tenant_id=tenant.id,
            provider_connection_id=connection.id,
            channel="whatsapp",
            sender_type="whatsapp_business",
            phone_number=sender_phone,
            sender_id=f"whatsapp:{sender_phone}",
            status="online",
            status_callback_url="https://api.example.test/twilio/whatsapp/status",
        )
    )
    db.session.add(
        WhatsAppContactState(
            tenant_id=tenant.id,
            recipient=REQUESTER_PHONE,
            last_inbound_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()


def _post_location(client, app, scope, *, actor: User, key: str):
    return client.post(
        "/api/v2/inbox/omnichannel/actions",
        json=_location_payload(scope.ticket.id),
        headers=_auth_headers(app, actor, scope.tenant, key),
    )


def _tenant_counts(scope) -> dict[str, int]:
    tenant_id = scope.tenant.id
    ticket_id = scope.ticket.id
    return {
        "comments": TicketComentario.query.filter_by(
            municipio_ticket_id=ticket_id
        ).count(),
        "receipts": TicketDomainEffectReceipt.query.filter_by(
            tenant_id=tenant_id
        ).count(),
        "audit_events": AuditEvent.query.filter_by(
            tenant_id=tenant_id,
            event_type=ACTION_AUDIT_EVENT_TYPE,
            resource_type=ACTION_AUDIT_RESOURCE_TYPE,
        ).count(),
        "outbox": DomainEffectOutbox.query.filter_by(tenant_id=tenant_id).count(),
        "notifications": Notification.query.filter_by(tenant_id=tenant_id).count(),
        "attempts": NotificationAttempt.query.filter_by(tenant_id=tenant_id).count(),
    }


def test_internal_only_commits_comment_and_authoritative_audit_event(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_internal_only(app, monkeypatch)

    response = _post_location(
        client,
        app,
        scope,
        actor=scope.owner,
        key="endpoint-location-internal-0001",
    )

    assert response.status_code == 200, response.get_json()
    delivery = response.get_json()["delivery"]
    assert delivery["mode"] == "internal_event"
    assert delivery["status"] == "recorded_in_crm"
    assert delivery["reason"] == "domain_effect_outbox_disabled"
    assert delivery["final_delivery"]["status"] == "not_dispatched"
    assert delivery["outbox"]["durably_staged"] is False
    assert delivery["external_dispatch"] is False

    comment = TicketComentario.query.filter_by(
        municipio_ticket_id=scope.ticket.id
    ).one()
    audit_event = AuditEvent.query.filter_by(
        tenant_id=scope.tenant.id,
        event_type=ACTION_AUDIT_EVENT_TYPE,
        resource_type=ACTION_AUDIT_RESOURCE_TYPE,
    ).one()
    comment_id = comment.id
    audit_event_id = audit_event.id
    assert comment.origen == "internal"
    assert comment.es_admin is True
    assert audit_event.actor_user_id == scope.owner.id
    assert audit_event.resource_id == f"{scope.ticket.id}:comment:{comment.id}"
    assert audit_event.details["tenant_id"] == scope.tenant.id
    assert audit_event.details["ticket_id"] == scope.ticket.id
    assert audit_event.details["comment_id"] == comment.id
    assert audit_event.details["action"] == "share_location"
    assert audit_event.details["action_payload"]["lat"] == -34.58333
    assert _tenant_counts(scope) == {
        "comments": 1,
        "receipts": 1,
        "audit_events": 1,
        "outbox": 0,
        "notifications": 0,
        "attempts": 0,
    }

    # A fresh scoped session proves both records were committed, not merely flushed.
    db.session.remove()
    assert db.session.get(TicketComentario, comment_id) is not None
    assert db.session.get(AuditEvent, audit_event_id) is not None


def test_location_endpoint_stages_durable_delivery_without_provider_io(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_durable_location_delivery(app, monkeypatch, scope)

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as provider_send:
        response = _post_location(
            client,
            app,
            scope,
            actor=scope.owner,
            key="endpoint-location-durable-0001",
        )

    assert response.status_code == 200, response.get_json()
    provider_send.assert_not_called()
    delivery = response.get_json()["delivery"]
    assert delivery["mode"] == "durable_queue"
    assert delivery["status"] == "durably_staged"
    assert delivery["reason"] == "domain_effect_durably_staged"
    assert delivery["external_dispatch"] is False
    assert delivery["final_delivery"] == {
        "status": "pending_provider_callback",
        "authoritative_source": "provider_status_callback",
    }
    assert delivery["outbox"]["effect_count"] == 1
    assert delivery["outbox"]["worker_authoritative"] is True
    assert delivery["outbox"]["direct_dispatch_performed"] is False
    timeline_action = next(
        item
        for item in response.get_json()["ticket"]["timeline"]
        if item.get("action") == "share_location"
    )
    assert timeline_action["delivery_mode"] == "durable_queue"
    assert timeline_action["external_dispatch"] is False

    audit_event = AuditEvent.query.filter_by(
        tenant_id=scope.tenant.id,
        event_type=ACTION_AUDIT_EVENT_TYPE,
    ).one()
    effect = DomainEffectOutbox.query.filter_by(tenant_id=scope.tenant.id).one()
    notification = Notification.query.filter_by(tenant_id=scope.tenant.id).one()
    attempt = NotificationAttempt.query.filter_by(tenant_id=scope.tenant.id).one()
    assert effect.aggregate_type == "municipio_crm_action"
    assert effect.aggregate_ref == str(audit_event.id)
    assert effect.channel == "whatsapp"
    assert effect.status == DomainEffectOutbox.STATUS_PENDING
    assert set(effect.payload_json) == {
        "action_binding",
        "provider_sender_binding",
        "destination_binding",
    }
    opaque_payload = json.dumps(effect.payload_json, sort_keys=True)
    assert REQUESTER_PHONE not in opaque_payload
    assert "-34.58333" not in opaque_payload
    assert notification.status == Notification.STATUS_BLOCKED
    assert attempt.status == NotificationAttempt.STATUS_BLOCKED
    assert attempt.notification_id == notification.id
    assert notification.metadata_json["audit_event_id"] == audit_event.id
    assert attempt.metadata_json["audit_event_id"] == audit_event.id
    assert _tenant_counts(scope) == {
        "comments": 1,
        "receipts": 1,
        "audit_events": 1,
        "outbox": 1,
        "notifications": 1,
        "attempts": 1,
    }


def test_staging_failure_rolls_back_comment_audit_receipt_and_bounded_ui_record(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_internal_only(app, monkeypatch)

    with patch(
        "services.ticket_crm_action_delivery.stage_legacy_ticket_crm_action_delivery",
        side_effect=RuntimeError("forced endpoint staging failure"),
    ):
        response = _post_location(
            client,
            app,
            scope,
            actor=scope.owner,
            key="endpoint-location-rollback-0001",
        )

    assert response.status_code == 503, response.get_json()
    assert (
        response.get_json()["reason_code"]
        == "crm_action_delivery_temporarily_unavailable"
    )
    assert _tenant_counts(scope) == {
        "comments": 0,
        "receipts": 0,
        "audit_events": 0,
        "outbox": 0,
        "notifications": 0,
        "attempts": 0,
    }
    persisted_ticket = db.session.get(MunicipioTicket, scope.ticket.id)
    assert not (persisted_ticket.datos_extra or {}).get("crm_reply_actions")


def test_durable_endpoint_replay_is_idempotent_without_duplicate_records_or_io(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_durable_location_delivery(app, monkeypatch, scope)

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as provider_send:
        first = _post_location(
            client,
            app,
            scope,
            actor=scope.owner,
            key="endpoint-location-replay-0001",
        )
        replay = _post_location(
            client,
            app,
            scope,
            actor=scope.owner,
            key="endpoint-location-replay-0001",
        )

    assert first.status_code == 200, first.get_json()
    assert replay.status_code == 200, replay.get_json()
    provider_send.assert_not_called()
    assert first.get_json()["delivery"]["idempotency"]["replayed"] is False
    replay_delivery = replay.get_json()["delivery"]
    assert replay_delivery["idempotency"]["replayed"] is True
    assert replay_delivery["mode"] == "durable_queue"
    assert replay_delivery["reason"] == "idempotent_replay_domain_effect_preserved"
    assert replay_delivery["outbox"]["idempotent_replay"] is True
    assert replay_delivery["outbox"]["effect_count"] == 1
    assert _tenant_counts(scope) == {
        "comments": 1,
        "receipts": 1,
        "audit_events": 1,
        "outbox": 1,
        "notifications": 1,
        "attempts": 1,
    }


def test_unassigned_employee_is_blocked_and_transaction_is_rolled_back(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_internal_only(app, monkeypatch)
    scope.ticket.asignado_a_id = None
    db.session.add(scope.ticket)
    db.session.commit()

    response = _post_location(
        client,
        app,
        scope,
        actor=scope.employee,
        key="endpoint-location-unassigned-0001",
    )

    assert response.status_code == 409, response.get_json()
    assert response.get_json()["reason_code"] == "crm_action_employee_assignment_required"
    assert _tenant_counts(scope) == {
        "comments": 0,
        "receipts": 0,
        "audit_events": 0,
        "outbox": 0,
        "notifications": 0,
        "attempts": 0,
    }
    persisted_ticket = db.session.get(MunicipioTicket, scope.ticket.id)
    assert persisted_ticket.asignado_a_id is None
    assert not (persisted_ticket.datos_extra or {}).get("crm_reply_actions")


def test_internal_only_replay_never_turns_into_a_delayed_external_send(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    key = "endpoint-location-frozen-internal-0001"
    _enable_internal_only(app, monkeypatch)

    first = _post_location(client, app, scope, actor=scope.owner, key=key)
    assert first.status_code == 200, first.get_json()
    assert first.get_json()["delivery"]["mode"] == "internal_event"

    _enable_durable_location_delivery(app, monkeypatch, scope)
    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as provider_send:
        replay = _post_location(client, app, scope, actor=scope.owner, key=key)

    assert replay.status_code == 200, replay.get_json()
    provider_send.assert_not_called()
    delivery = replay.get_json()["delivery"]
    assert delivery["idempotency"]["replayed"] is True
    assert delivery["mode"] == "internal_event"
    assert delivery["status"] == "already_recorded"
    assert delivery["reason"] == "idempotent_replay_internal_only_preserved"
    assert _tenant_counts(scope) == {
        "comments": 1,
        "receipts": 1,
        "audit_events": 1,
        "outbox": 0,
        "notifications": 0,
        "attempts": 0,
    }


def test_address_only_location_is_audited_but_not_queued_as_geo(
    app,
    client,
    monkeypatch,
    municipal_crm_scope,
):
    scope = municipal_crm_scope
    _enable_durable_location_delivery(app, monkeypatch, scope)
    payload = _location_payload(scope.ticket.id)
    payload["location"] = {
        "address": "Plaza 25 de Mayo, Junín",
        "label": "Punto de atención municipal",
    }

    with patch(
        "services.ticket_crm_action_delivery.send_prepared_tenant_twilio_message"
    ) as provider_send:
        response = client.post(
            "/api/v2/inbox/omnichannel/actions",
            json=payload,
            headers=_auth_headers(
                app,
                scope.owner,
                scope.tenant,
                "endpoint-location-address-only-0001",
            ),
        )

    assert response.status_code == 200, response.get_json()
    provider_send.assert_not_called()
    delivery = response.get_json()["delivery"]
    assert delivery["mode"] == "internal_event"
    assert delivery["reason"] == "whatsapp_location_coordinates_required"
    assert delivery["final_delivery"]["status"] == "not_dispatched"
    audit_event = AuditEvent.query.filter_by(
        tenant_id=scope.tenant.id,
        event_type=ACTION_AUDIT_EVENT_TYPE,
    ).one()
    assert audit_event.details["action_payload"] == {
        "lat": None,
        "lng": None,
        "address": "Plaza 25 de Mayo, Junín",
        "label": "Punto de atención municipal",
    }
    assert _tenant_counts(scope) == {
        "comments": 1,
        "receipts": 1,
        "audit_events": 1,
        "outbox": 0,
        "notifications": 0,
        "attempts": 0,
    }
