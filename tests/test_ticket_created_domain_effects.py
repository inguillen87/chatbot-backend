from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from models import (
    DomainEffectOutbox,
    MunicipioTicket,
    PymeTicket,
    Rubro,
    TenantProfile,
    TicketDomainEffectReceipt,
    User,
    db,
)
from services.domain_effect_outbox import dispatch_domain_effects
from services.domain_effect_gate import DomainEffectOutboxConfigurationError
from services.ticket_domain_effects import (
    ADMIN_EMAIL_HANDLER,
    REQUESTER_EMAIL_HANDLER,
    SIGEM_HANDLER,
    TICKET_DOMAIN_EFFECT_REGISTRY,
)
from services.ticket_service import ServicioTickets
from services.tenant_ticket_scope import TicketTenantScopeError


SECRET = "outbox-test-secret-" + ("x" * 32)


@pytest.fixture
def effect_tenants(init_database, owner_user):
    municipal = TenantProfile(
        slug="ticket-outbox-municipal",
        nombre="Municipio Outbox",
        tipo="municipio",
        municipio_id=owner_user.id,
    )
    db.session.add(municipal)
    db.session.flush()
    owner_user.tenant_id = municipal.id

    rubro = Rubro.query.filter_by(clave="ticket-outbox-pyme").first()
    if rubro is None:
        rubro = Rubro(clave="ticket-outbox-pyme", nombre="PyME Outbox")
        db.session.add(rubro)
        db.session.flush()
    pyme_owner = User(
        name="PyME Admin",
        email="pyme-admin@example.com",
        rol="admin",
        tipo_chat="pyme",
        rubro_id=rubro.id,
    )
    pyme_owner.set_password("test-password")
    db.session.add(pyme_owner)
    db.session.flush()
    pyme = TenantProfile(
        slug="ticket-outbox-pyme",
        nombre="PyME Outbox",
        tipo="pyme",
        pyme_id=pyme_owner.id,
    )
    db.session.add(pyme)
    db.session.flush()
    pyme_owner.tenant_id = pyme.id
    db.session.commit()
    return municipal, pyme, pyme_owner, rubro


def _set_queue(app, monkeypatch, tenant_id: int) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", SECRET)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", str(tenant_id))
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_PAYLOAD_BYTES", 4096)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MAX_ATTEMPTS", 8)


def _set_legacy(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", "")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", "")


def _set_email_provider_config(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setitem(app.config, "SMTP_HOST", "smtp.test")
    monkeypatch.setitem(app.config, "SMTP_PORT", 587)
    monkeypatch.setitem(app.config, "SMTP_USER", "smtp-user")
    monkeypatch.setitem(app.config, "SMTP_PASSWORD", "smtp-password")
    monkeypatch.setitem(app.config, "MAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setitem(app.config, "SMTP_REQUIRE_AUTH", True)


def _municipal_payload(tenant, owner_user, **overrides):
    payload = {
        "tenant_id": tenant.id,
        "municipio_id": owner_user.id,
        "asunto": "Luminaria rota",
        "categoria": "Luminaria",
        "pregunta": "La luminaria no enciende",
        "detalles": "Poste frente a una vivienda privada",
        "direccion": "Calle Privada 123",
        "nombre_vecino": "Persona Confidencial",
        "telefono_vecino": "+5492613111111",
        "email_vecino": "ciudadano-secreto@example.com",
        "dni_vecino": "32999999",
        "consulta_pin": "827364",
    }
    payload.update(overrides)
    return payload


def _pyme_payload(tenant, owner, rubro, **overrides):
    payload = {
        "tenant_id": tenant.id,
        "pyme_id": owner.id,
        "rubro_id": rubro.id,
        "asunto": "Consulta de entrega",
        "categoria": "Pedido",
        "pregunta": "Necesito coordinar la entrega",
        "telefono_cliente": "+5492613222222",
        "email_cliente": "comprador-secreto@example.com",
        "dni": "30111222",
        "consulta_pin": "192837",
    }
    payload.update(overrides)
    return payload


def _service() -> ServicioTickets:
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    return service


def test_canary_municipal_ticket_stages_three_rows_and_never_dual_sends(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    service = _service()

    with patch("services.ticket_service.enviar_ticket_a_sigem") as legacy_sigem, patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ) as wakeup:
        ticket = service.crear_nuevo_ticket(
            "municipio",
            _municipal_payload(municipal, owner_user),
        )

    assert ticket is not None
    rows = DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).order_by(
        DomainEffectOutbox.id
    ).all()
    assert [row.handler_name for row in rows] == [
        SIGEM_HANDLER,
        ADMIN_EMAIL_HANDLER,
        REQUESTER_EMAIL_HANDLER,
    ]
    assert [row.channel for row in rows] == ["sigem", "email", "email"]
    assert all(row.status == DomainEffectOutbox.STATUS_PENDING for row in rows)
    assert all(set(row.payload_json) == {"owner_binding"} for row in rows)
    assert len({row.payload_json["owner_binding"] for row in rows}) == 1
    assert len(rows[0].payload_json["owner_binding"]) == 64
    assert {row.recipient_ref for row in rows} == {
        "integration:tenant.sigem",
        "role:tenant.admins",
        "role:ticket.requester",
    }
    legacy_sigem.assert_not_called()
    wakeup.assert_called_once_with(tenant_id=municipal.id)
    service._notificar_ticket_por_email.assert_not_called()

    serialized_rows = json.dumps(
        [
            {
                "aggregate_type": row.aggregate_type,
                "aggregate_ref": row.aggregate_ref,
                "effect_type": row.effect_type,
                "handler_name": row.handler_name,
                "channel": row.channel,
                "recipient_ref": row.recipient_ref,
                "effect_key": row.effect_key,
                "intent_hmac": row.intent_hmac,
                "payload_json": row.payload_json,
            }
            for row in rows
        ],
        sort_keys=True,
    )
    for pii in (
        "Persona Confidencial",
        "+5492613111111",
        "ciudadano-secreto@example.com",
        "32999999",
        "Calle Privada 123",
        "827364",
    ):
        assert pii not in serialized_rows


def test_canary_pyme_ticket_stages_two_email_rows_without_legacy_email(
    app,
    monkeypatch,
    effect_tenants,
):
    _, pyme, pyme_owner, rubro = effect_tenants
    _set_queue(app, monkeypatch, pyme.id)
    service = _service()

    ticket = service.crear_nuevo_ticket(
        "pyme",
        _pyme_payload(pyme, pyme_owner, rubro),
    )

    assert ticket is not None
    rows = DomainEffectOutbox.query.filter_by(tenant_id=pyme.id).order_by(
        DomainEffectOutbox.id
    ).all()
    assert [row.handler_name for row in rows] == [
        ADMIN_EMAIL_HANDLER,
        REQUESTER_EMAIL_HANDLER,
    ]
    assert all(set(row.payload_json) == {"owner_binding"} for row in rows)
    assert "comprador-secreto@example.com" not in json.dumps(
        [row.recipient_ref for row in rows]
    )
    service._notificar_ticket_por_email.assert_not_called()


def test_staging_failure_rolls_back_ticket_receipt_and_outbox(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    service = _service()

    with patch(
        "services.ticket_domain_effects.stage_domain_effect",
        side_effect=RuntimeError("staging unavailable"),
    ), pytest.raises(RuntimeError, match="staging unavailable"):
        service.crear_nuevo_ticket(
            "municipio",
            _municipal_payload(municipal, owner_user),
            idempotency_key="external:ticket:rollback-0001",
            idempotency_tenant_id=municipal.id,
        )

    assert MunicipioTicket.query.filter_by(tenant_id=municipal.id).count() == 0
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=municipal.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).count() == 0


@pytest.mark.parametrize("ticket_type", ["municipio", "pyme"])
def test_canary_rejects_cross_tenant_owner_binding_before_commit(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
    ticket_type,
):
    municipal, pyme, pyme_owner, rubro = effect_tenants
    tenant = municipal if ticket_type == "municipio" else pyme
    _set_queue(app, monkeypatch, tenant.id)
    service = _service()
    payload = (
        _municipal_payload(municipal, owner_user, municipio_id=pyme_owner.id)
        if ticket_type == "municipio"
        else _pyme_payload(pyme, pyme_owner, rubro, pyme_id=owner_user.id)
    )

    if ticket_type == "municipio":
        # Municipal writes are rejected by the authoritative scope boundary
        # before the generic outbox canary is reached.
        with pytest.raises(TicketTenantScopeError) as exc_info:
            service.crear_nuevo_ticket(ticket_type, payload)
        assert exc_info.value.code == "ticket_tenant_owner_mismatch"
    else:
        with pytest.raises(
            DomainEffectOutboxConfigurationError,
            match="domain_effect_ticket_tenant_binding_invalid",
        ):
            service.crear_nuevo_ticket(ticket_type, payload)

    model = MunicipioTicket if ticket_type == "municipio" else PymeTicket
    assert model.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_legacy_tenant_preserves_direct_sigem_and_email_behavior(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_legacy(app, monkeypatch)
    service = _service()

    with patch(
        "services.ticket_service.enviar_ticket_a_sigem",
        return_value=True,
    ) as legacy_sigem:
        ticket = service.crear_nuevo_ticket(
            "municipio",
            _municipal_payload(municipal, owner_user),
        )

    assert ticket is not None
    legacy_sigem.assert_called_once()
    service._notificar_ticket_por_email.assert_called_once()
    assert DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).count() == 0


def test_idempotent_replay_does_not_duplicate_queued_effects(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    service = _service()
    key = "external:ticket:queue-replay-0001"

    with patch("services.ticket_service.enviar_ticket_a_sigem") as legacy_sigem:
        first = service.crear_nuevo_ticket(
            "municipio",
            _municipal_payload(municipal, owner_user),
            idempotency_key=key,
            idempotency_tenant_id=municipal.id,
        )
        replay = service.crear_nuevo_ticket(
            "municipio",
            _municipal_payload(municipal, owner_user, consulta_pin="999999"),
            idempotency_key=key,
            idempotency_tenant_id=municipal.id,
        )

    assert replay == first
    assert MunicipioTicket.query.filter_by(tenant_id=municipal.id).count() == 1
    assert DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).count() == 3
    legacy_sigem.assert_not_called()
    service._notificar_ticket_por_email.assert_not_called()


def test_worker_preflight_skips_missing_config_before_io_and_sigem_stub(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", False)
    # Even when the flag is forced in tests, the current log-only SIGEM adapter
    # must be recognized as unavailable before io_started.
    monkeypatch.setitem(app.config, "SIGEM_LIVE_ENABLED", True)
    service = _service()
    service.crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )

    dispatch_domain_effects(
        registry=TICKET_DOMAIN_EFFECT_REGISTRY,
        intent_secret=SECRET,
        tenant_id=municipal.id,
        limit=10,
    )

    rows = DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).all()
    assert len(rows) == 3
    assert all(row.status == DomainEffectOutbox.STATUS_SKIPPED for row in rows)
    assert all(row.io_started_at is None for row in rows)
    by_handler = {row.handler_name: row for row in rows}
    assert by_handler[SIGEM_HANDLER].result_json == {
        "reason_code": "sigem_transport_unavailable"
    }
    assert by_handler[ADMIN_EMAIL_HANDLER].result_json == {
        "reason_code": "email_notifications_disabled"
    }
    assert (
        by_handler[REQUESTER_EMAIL_HANDLER].result_json
        == {"reason_code": "email_notifications_disabled"}
    )


def test_worker_skips_invalid_created_email_recipients_before_provider_preflight(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    _set_email_provider_config(app, monkeypatch)
    _service().crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )
    ticket = MunicipioTicket.query.filter_by(tenant_id=municipal.id).one()
    owner_user.email = "admin-address-invalid"
    ticket.email_vecino = "requester-address-invalid"
    db.session.commit()

    with patch(
        "services.email_service.validar_configuracion_smtp"
    ) as smtp_preflight, patch(
        "services.email_service.enviar_email_ticket_admin"
    ) as admin_sender, patch(
        "services.email_service.enviar_email_ticket_cliente"
    ) as requester_sender:
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=municipal.id,
            limit=10,
        )

    rows = {
        row.handler_name: row
        for row in DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).all()
    }
    assert rows[ADMIN_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[ADMIN_EMAIL_HANDLER].io_started_at is None
    assert rows[ADMIN_EMAIL_HANDLER].result_json == {
        "reason_code": "admin_recipient_invalid"
    }
    assert rows[REQUESTER_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[REQUESTER_EMAIL_HANDLER].io_started_at is None
    assert rows[REQUESTER_EMAIL_HANDLER].result_json == {
        "reason_code": "requester_recipient_invalid"
    }
    smtp_preflight.assert_not_called()
    admin_sender.assert_not_called()
    requester_sender.assert_not_called()


def test_worker_validates_dispatch_email_and_preserves_missing_requester_reason(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    _set_email_provider_config(app, monkeypatch)
    _service().crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )
    ticket = MunicipioTicket.query.filter_by(tenant_id=municipal.id).one()
    owner_user.email = ""
    municipal.dispatch_email = "dispatch-address-invalid"
    ticket.email_vecino = ""
    db.session.commit()

    with patch(
        "services.email_service.validar_configuracion_smtp"
    ) as smtp_preflight, patch(
        "services.email_service.enviar_email_ticket_admin"
    ) as admin_sender, patch(
        "services.email_service.enviar_email_ticket_cliente"
    ) as requester_sender:
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=municipal.id,
            limit=10,
        )

    rows = {
        row.handler_name: row
        for row in DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).all()
    }
    assert rows[ADMIN_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[ADMIN_EMAIL_HANDLER].io_started_at is None
    assert rows[ADMIN_EMAIL_HANDLER].result_json == {
        "reason_code": "admin_recipient_invalid"
    }
    assert rows[REQUESTER_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[REQUESTER_EMAIL_HANDLER].io_started_at is None
    assert rows[REQUESTER_EMAIL_HANDLER].result_json == {
        "reason_code": "requester_recipient_missing"
    }
    smtp_preflight.assert_not_called()
    admin_sender.assert_not_called()
    requester_sender.assert_not_called()


def test_worker_fails_closed_if_tenant_owner_changes_before_delivery(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, pyme_owner, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    service = _service()
    service.crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )
    municipal.municipio_id = pyme_owner.id
    db.session.commit()

    with patch("services.integracion_municipal.enviar_ticket_a_sigem") as sigem, patch(
        "services.email_service.enviar_email_ticket_admin"
    ) as admin_sender, patch(
        "services.email_service.enviar_email_ticket_cliente"
    ) as requester_sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=municipal.id,
            limit=10,
        )

    rows = DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).all()
    assert summary.dead == 3
    assert all(row.status == DomainEffectOutbox.STATUS_DEAD for row in rows)
    assert all(row.io_started_at is None for row in rows)
    assert all(row.last_error_code == "ticket_tenant_binding_invalid" for row in rows)
    sigem.assert_not_called()
    admin_sender.assert_not_called()
    requester_sender.assert_not_called()


def test_worker_fails_closed_if_owner_user_belongs_to_another_tenant(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, pyme, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    _set_email_provider_config(app, monkeypatch)
    service = _service()
    service.crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )

    # The tenant profile and immutable outbox binding still reference this
    # owner ID, but the recipient User has moved to another tenant.
    owner_user.tenant_id = pyme.id
    db.session.commit()

    with patch("services.integracion_municipal.enviar_ticket_a_sigem") as sigem, patch(
        "services.email_service.enviar_email_ticket_admin"
    ) as admin_sender, patch(
        "services.email_service.enviar_email_ticket_cliente"
    ) as requester_sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=municipal.id,
            limit=10,
        )

    email_rows = DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == municipal.id,
        DomainEffectOutbox.handler_name.in_(
            [ADMIN_EMAIL_HANDLER, REQUESTER_EMAIL_HANDLER]
        ),
    ).all()
    assert summary.dead == 2
    assert len(email_rows) == 2
    assert all(row.status == DomainEffectOutbox.STATUS_DEAD for row in email_rows)
    assert all(row.io_started_at is None for row in email_rows)
    assert all(
        row.last_error_code == "ticket_admin_tenant_binding_invalid"
        for row in email_rows
    )
    sigem.assert_not_called()
    admin_sender.assert_not_called()
    requester_sender.assert_not_called()


def test_provider_false_after_io_is_unknown_not_success(
    app,
    monkeypatch,
    effect_tenants,
    owner_user,
):
    municipal, _, _, _ = effect_tenants
    _set_queue(app, monkeypatch, municipal.id)
    monkeypatch.setitem(app.config, "SIGEM_LIVE_ENABLED", False)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setitem(app.config, "SMTP_HOST", "smtp.test")
    monkeypatch.setitem(app.config, "SMTP_PORT", 587)
    monkeypatch.setitem(app.config, "SMTP_USER", "smtp-user")
    monkeypatch.setitem(app.config, "SMTP_PASSWORD", "smtp-password")
    monkeypatch.setitem(app.config, "MAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setitem(app.config, "SMTP_REQUIRE_AUTH", True)
    service = _service()
    service.crear_nuevo_ticket(
        "municipio",
        _municipal_payload(municipal, owner_user),
    )

    with patch(
        "services.email_service.enviar_email_ticket_admin",
        return_value=False,
    ) as admin_sender, patch(
        "services.email_service.enviar_email_ticket_cliente",
        return_value=True,
    ) as requester_sender:
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=municipal.id,
            limit=10,
        )

    rows = {
        row.handler_name: row
        for row in DomainEffectOutbox.query.filter_by(tenant_id=municipal.id).all()
    }
    assert rows[SIGEM_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[SIGEM_HANDLER].io_started_at is None
    assert rows[ADMIN_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_UNKNOWN
    assert rows[ADMIN_EMAIL_HANDLER].io_started_at is not None
    assert rows[REQUESTER_EMAIL_HANDLER].status == DomainEffectOutbox.STATUS_SUCCEEDED
    assert rows[REQUESTER_EMAIL_HANDLER].io_started_at is not None
    assert rows[REQUESTER_EMAIL_HANDLER].result_json == {
        "delivery": "provider_accepted"
    }
    admin_sender.assert_called_once()
    requester_sender.assert_called_once()
