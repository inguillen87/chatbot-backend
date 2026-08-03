from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from models import (
    ArchivoAdjunto,
    DomainEffectOutbox,
    MunicipioTicket,
    ProviderConnection,
    ProviderSender,
    PymeTicket,
    Rubro,
    TenantProfile,
    TicketComentario,
    TicketDomainEffectReceipt,
    User,
    db,
)
from services.domain_effect_gate import DomainEffectOutboxConfigurationError
from services.domain_effect_outbox import (
    DomainEffectValidationError,
    dispatch_domain_effects,
    stage_domain_effect,
)
from services.ticket_domain_effects import (
    COMMENT_ADMIN_EMAIL_HANDLER,
    COMMENT_REALTIME_HANDLER,
    COMMENT_REQUESTER_EMAIL_HANDLER,
    COMMENT_REQUESTER_SMS_HANDLER,
    COMMENT_REQUESTER_WHATSAPP_HANDLER,
    MUNICIPAL_COMMENT_AGGREGATE,
    PYME_COMMENT_AGGREGATE,
    TICKET_DOMAIN_EFFECT_REGISTRY,
)
from services.ticket_service import ServicioTickets


SECRET = "comment-outbox-secret-" + ("x" * 32)


@pytest.fixture
def municipal_comment_context(init_database, owner_user):
    tenant = TenantProfile(
        slug="comment-effects-municipal",
        nombre="Comment Effects Municipal",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    owner_user.tenant_id = tenant.id
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner_user.id,
        pregunta="Reclamo con conversación",
        asunto="Luminaria",
        categoria="Luminaria",
        detalles="Detalle confidencial",
        nro_ticket="comment-100001",
        consulta_pin="482716",
        nombre_vecino="Persona Confidencial",
        email_vecino="requester-private@example.com",
        telefono_vecino="+5492613333333",
    )
    db.session.add(ticket)
    db.session.flush()
    attachment = ArchivoAdjunto(
        user_id=owner_user.id,
        municipio_ticket_id=ticket.id,
        filename="evidence.jpg",
        nombre_original="evidencia-privada.jpg",
        mime="image/jpeg",
        tamano=1234,
        tipo="imagen",
        url="https://files.example.test/private/evidence.jpg",
    )
    db.session.add(attachment)
    db.session.commit()
    return tenant, ticket, attachment


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


def _set_provider_config(app, monkeypatch, tenant=None):
    # Provider SDKs remain offline by default under TESTING.  This suite uses
    # explicit mocks and opts Twilio in only inside its local app config.
    monkeypatch.setitem(app.config, "TWILIO_ALLOW_NETWORK_IN_TESTS", True)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setitem(app.config, "SMTP_HOST", "smtp.test")
    monkeypatch.setitem(app.config, "SMTP_PORT", 587)
    monkeypatch.setitem(app.config, "SMTP_USER", "smtp-user")
    monkeypatch.setitem(app.config, "SMTP_PASSWORD", "smtp-password")
    monkeypatch.setitem(app.config, "MAIL_FROM_ADDRESS", "noreply@test.local")
    monkeypatch.setitem(app.config, "SMTP_REQUIRE_AUTH", True)
    # Poisoned deployment-wide credentials remain configured to prove that
    # queue-mode ticket effects never use them as an implicit tenant sender.
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "AC-global-parent")
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "global-token")
    monkeypatch.setitem(app.config, "TWILIO_PHONE_NUMBER", "+15005550006")
    monkeypatch.setitem(
        app.config,
        "TWILIO_WHATSAPP_NUMBER",
        "whatsapp:+15005550006",
    )
    if tenant is None:
        return None

    account_sid = f"AC-tenant-{tenant.id}"
    token_ref = f"TWILIO_SUBACCOUNT_AUTH_TOKEN_COMMENT_TENANT_{tenant.id}"
    auth_token = f"tenant-token-{tenant.id}"
    monkeypatch.setitem(app.config, token_ref, auth_token)
    tenant.configuracion = {
        **(tenant.configuracion or {}),
        "twilio_tech_provider": {
            "twilio_account_sid": account_sid,
            "twilio_subaccount_token_ref": token_ref,
        },
    }
    senders = {}
    for channel in ("sms", "whatsapp"):
        connection = ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel=channel,
            environment="production",
            status="online",
            external_account_id=account_sid,
            credentials_ref=f"env:{token_ref}",
        )
        db.session.add(connection)
        db.session.flush()
        phone = f"+1500555{tenant.id:04d}"
        sender = ProviderSender(
            tenant_id=tenant.id,
            provider_connection_id=connection.id,
            channel=channel,
            sender_type=(
                "whatsapp_business" if channel == "whatsapp" else "phone_number"
            ),
            phone_number=phone,
            sender_id=(f"whatsapp:{phone}" if channel == "whatsapp" else phone),
            status="online",
            status_callback_url=(
                f"https://api.example.test/twilio/{channel}/status"
            ),
        )
        db.session.add(sender)
        senders[channel] = sender
    db.session.commit()
    return {
        "account_sid": account_sid,
        "auth_token": auth_token,
        "senders": senders,
    }


def _admin_comment_payload(attachment_id=None):
    return {
        "comentario": "La cuadrilla llegará mañana por la mañana",
        "user_id": 1,
        "es_admin": True,
        "origen": "chat",
        "archivo_adjunto_id": attachment_id,
    }


def test_admin_comment_stages_each_channel_once_and_replays_with_attachment(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, attachment = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    twilio_scope = _set_provider_config(app, monkeypatch, tenant)
    service = ServicioTickets()
    key = "external:comment:attachment-replay-0001"

    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ) as email_sender, patch(
        "services.email_service.enviar_sms_ticket_novedad",
        return_value=True,
    ) as sms_sender, patch(
        "services.email_service.enviar_whatsapp_ticket_novedad",
        return_value=True,
    ) as whatsapp_sender, patch(
        "socket_service.emit_new_chat_message"
    ) as socket_sender, patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=True,
    ) as enqueue_dispatch, patch(
        "services.tenant_twilio_messaging.Client"
    ) as twilio_client:
        twilio_client.return_value.messages.create.side_effect = [
            SimpleNamespace(sid="SM-comment-sms"),
            SimpleNamespace(sid="SM-comment-whatsapp"),
        ]
        comment = service.crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(attachment.id),
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )

        assert comment is not None
        email_sender.assert_not_called()
        sms_sender.assert_not_called()
        whatsapp_sender.assert_not_called()
        socket_sender.assert_not_called()
        enqueue_dispatch.assert_called_once_with(tenant_id=tenant.id)

        rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).order_by(
            DomainEffectOutbox.id
        ).all()
        assert [row.handler_name for row in rows] == [
            COMMENT_REQUESTER_EMAIL_HANDLER,
            COMMENT_REQUESTER_SMS_HANDLER,
            COMMENT_REQUESTER_WHATSAPP_HANDLER,
            COMMENT_REALTIME_HANDLER,
        ]
        payload_keys = {
            row.handler_name: set(row.payload_json)
            for row in rows
        }
        assert payload_keys[COMMENT_REQUESTER_EMAIL_HANDLER] == {"owner_binding"}
        assert payload_keys[COMMENT_REALTIME_HANDLER] == {"owner_binding"}
        assert payload_keys[COMMENT_REQUESTER_SMS_HANDLER] == {
            "owner_binding",
            "provider_sender_binding",
        }
        assert payload_keys[COMMENT_REQUESTER_WHATSAPP_HANDLER] == {
            "owner_binding",
            "provider_sender_binding",
        }
        assert all(len(row.payload_json["owner_binding"]) == 64 for row in rows)
        assert all(
            len(row.payload_json["provider_sender_binding"]) == 64
            for row in rows
            if row.channel in {"sms", "whatsapp"}
        )
        assert all(row.aggregate_type == MUNICIPAL_COMMENT_AGGREGATE for row in rows)
        assert all(row.aggregate_ref == str(comment.id) for row in rows)

        serialized = json.dumps(
            [
                {
                    "effect_key": row.effect_key,
                    "recipient_ref": row.recipient_ref,
                    "payload": row.payload_json,
                }
                for row in rows
            ],
            sort_keys=True,
        )
        for pii in (
            "La cuadrilla",
            "requester-private@example.com",
            "+5492613333333",
            "evidencia-privada.jpg",
            "files.example.test",
        ):
            assert pii not in serialized

        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )
        email_sender.assert_called_once()
        sms_sender.assert_not_called()
        whatsapp_sender.assert_not_called()
        assert twilio_client.call_count == 2
        assert all(
            args.args == (twilio_scope["account_sid"], twilio_scope["auth_token"])
            for args in twilio_client.call_args_list
        )
        socket_sender.assert_called_once()
        completed_rows = {
            row.handler_name: row
            for row in DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).all()
        }
        for handler_name in (
            COMMENT_REQUESTER_EMAIL_HANDLER,
            COMMENT_REQUESTER_SMS_HANDLER,
            COMMENT_REQUESTER_WHATSAPP_HANDLER,
        ):
            assert completed_rows[handler_name].result_json == {
                "delivery": "provider_accepted"
            }
        assert completed_rows[COMMENT_REALTIME_HANDLER].result_json == {
            "dispatch": "realtime_event_emitted"
        }
        twilio_calls = twilio_client.return_value.messages.create.call_args_list
        assert twilio_calls[0].kwargs["from_"] == twilio_scope["senders"]["sms"].phone_number
        assert twilio_calls[0].kwargs["to"] == "+5492613333333"
        assert twilio_calls[1].kwargs["from_"] == (
            f"whatsapp:{twilio_scope['senders']['whatsapp'].phone_number}"
        )
        assert twilio_calls[1].kwargs["to"] == "whatsapp:+5492613333333"
        assert twilio_calls[1].kwargs["media_url"] == [attachment.url]

        replay = service.crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(attachment.id),
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )
        enqueue_dispatch.assert_called_once_with(tenant_id=tenant.id)

    assert replay.id == comment.id
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 1
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 4
    rebound_attachment = db.session.get(ArchivoAdjunto, attachment.id)
    assert rebound_attachment.municipio_ticket_id == ticket.id
    assert rebound_attachment.pyme_ticket_id is None


def test_citizen_comment_queues_admin_email_and_realtime_without_dual_send(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    service = ServicioTickets()

    with patch(
        "services.email_service.enviar_email_ticket_admin",
        return_value=True,
    ) as admin_email, patch(
        "socket_service.emit_new_chat_message"
    ) as socket_sender:
        comment = service.crear_comentario(
            ticket.id,
            "municipio",
            {
                "comentario": "Sigue sin funcionar",
                "es_admin": False,
                "origen": "whatsapp",
            },
        )

    assert comment is not None
    assert [
        row.handler_name
        for row in DomainEffectOutbox.query.filter_by(tenant_id=tenant.id)
        .order_by(DomainEffectOutbox.id)
        .all()
    ] == [COMMENT_ADMIN_EMAIL_HANDLER, COMMENT_REALTIME_HANDLER]
    admin_email.assert_not_called()
    socket_sender.assert_not_called()


def test_partial_comment_staging_failure_rolls_back_comment_and_receipt(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, attachment = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    service = ServicioTickets()
    from services import ticket_domain_effects

    original_stage = ticket_domain_effects.stage_domain_effect
    call_count = 0

    def fail_after_second_insert(**kwargs):
        nonlocal call_count
        call_count += 1
        result = original_stage(**kwargs)
        if call_count == 2:
            raise RuntimeError("second comment effect staging failed")
        return result

    with patch.object(
        ticket_domain_effects,
        "stage_domain_effect",
        side_effect=fail_after_second_insert,
    ), pytest.raises(RuntimeError, match="second comment effect staging failed"):
        service.crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(attachment.id),
            idempotency_key="external:comment:rollback-0001",
            idempotency_tenant_id=tenant.id,
        )

    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 0
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=tenant.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    rebound_attachment = db.session.get(ArchivoAdjunto, attachment.id)
    assert rebound_attachment.municipio_ticket_id == ticket.id
    assert rebound_attachment.pyme_ticket_id is None


def test_queue_rejects_cross_owner_ticket_before_commit(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, attachment = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    other_owner = User(
        name="Other Owner",
        email="other-owner@example.com",
        rol="admin",
    )
    other_owner.set_password("test-password")
    db.session.add(other_owner)
    db.session.flush()
    ticket.municipio_id = other_owner.id
    db.session.commit()

    with pytest.raises(
        DomainEffectOutboxConfigurationError,
        match="domain_effect_comment_tenant_binding_invalid",
    ):
        ServicioTickets().crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(attachment.id),
        )

    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0


def test_queue_rejects_attachment_already_bound_to_another_ticket(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, attachment = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    other_ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        pregunta="Otro ticket",
        nro_ticket="comment-100002",
        consulta_pin="112233",
    )
    db.session.add(other_ticket)
    db.session.flush()
    attachment.municipio_ticket_id = other_ticket.id
    db.session.commit()

    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(attachment.id),
    )

    assert comment is None
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 0
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
    rebound_attachment = db.session.get(ArchivoAdjunto, attachment.id)
    assert rebound_attachment.municipio_ticket_id == other_ticket.id
    assert rebound_attachment.pyme_ticket_id is None


@pytest.mark.parametrize(
    ("enable_pyme_whatsapp", "expect_whatsapp"),
    [(True, True), (False, False)],
)
def test_pyme_admin_comment_honors_whatsapp_flag_and_dispatches_once(
    app,
    monkeypatch,
    init_database,
    enable_pyme_whatsapp,
    expect_whatsapp,
):
    rubro = Rubro(clave="comment-outbox-pyme", nombre="Comment Outbox PyME")
    pyme_owner = User(
        name="PyME Comment Admin",
        email="pyme-comment-admin@example.com",
        rol="admin",
        tipo_chat="pyme",
    )
    pyme_owner.set_password("test-password")
    db.session.add_all([rubro, pyme_owner])
    db.session.flush()
    pyme_owner.rubro_id = rubro.id
    tenant = TenantProfile(
        slug="comment-effects-pyme",
        nombre="Comment Effects PyME",
        tipo="pyme",
        pyme_id=pyme_owner.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    pyme_owner.tenant_id = tenant.id
    ticket = PymeTicket(
        tenant_id=tenant.id,
        pregunta="Consulta de entrega",
        asunto="Entrega",
        categoria="Pedido",
        nro_ticket=765432,
        consulta_pin="654321",
        rubro_id=rubro.id,
        email="pyme-requester-private@example.com",
        telefono="+5492613555555",
    )
    db.session.add(ticket)
    db.session.commit()
    _set_queue(app, monkeypatch, tenant.id)
    twilio_scope = _set_provider_config(app, monkeypatch, tenant)
    monkeypatch.setitem(
        app.config,
        "ENABLE_PYME_WHATSAPP_CHAT",
        enable_pyme_whatsapp,
    )

    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ) as email_sender, patch(
        "services.email_service.enviar_sms_ticket_novedad",
        return_value=True,
    ) as sms_sender, patch(
        "services.email_service.enviar_whatsapp_ticket_novedad",
        return_value=True,
    ) as whatsapp_sender, patch(
        "socket_service.emit_new_chat_message"
    ) as socket_sender, patch(
        "services.tenant_twilio_messaging.Client"
    ) as twilio_client:
        twilio_client.return_value.messages.create.side_effect = [
            SimpleNamespace(sid="SM-pyme-sms"),
            SimpleNamespace(sid="SM-pyme-whatsapp"),
        ]
        comment = ServicioTickets().crear_comentario(
            ticket.id,
            "pyme",
            _admin_comment_payload(),
        )
        assert comment is not None
        email_sender.assert_not_called()
        sms_sender.assert_not_called()
        whatsapp_sender.assert_not_called()
        socket_sender.assert_not_called()

        rows = DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).order_by(
            DomainEffectOutbox.id
        ).all()
        expected_handlers = [
            COMMENT_REQUESTER_EMAIL_HANDLER,
            COMMENT_REQUESTER_SMS_HANDLER,
        ]
        if expect_whatsapp:
            expected_handlers.append(COMMENT_REQUESTER_WHATSAPP_HANDLER)
        expected_handlers.append(COMMENT_REALTIME_HANDLER)
        assert [row.handler_name for row in rows] == expected_handlers
        assert all(row.aggregate_type == PYME_COMMENT_AGGREGATE for row in rows)

        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    email_sender.assert_called_once()
    sms_sender.assert_not_called()
    whatsapp_sender.assert_not_called()
    assert twilio_client.call_count == (2 if expect_whatsapp else 1)
    assert all(
        args.args == (twilio_scope["account_sid"], twilio_scope["auth_token"])
        for args in twilio_client.call_args_list
    )
    socket_sender.assert_called_once()


def test_missing_provider_config_skips_pre_io_but_false_ack_is_unknown(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", False)
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setitem(app.config, "TWILIO_PHONE_NUMBER", "")
    monkeypatch.setitem(app.config, "TWILIO_WHATSAPP_NUMBER", "")
    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(),
    )

    with patch("socket_service.emit_new_chat_message"):
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )
    skipped = DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == tenant.id,
        DomainEffectOutbox.handler_name != COMMENT_REALTIME_HANDLER,
    ).all()
    assert comment is not None
    assert len(skipped) == 3
    assert all(row.status == DomainEffectOutbox.STATUS_SKIPPED for row in skipped)
    assert all(row.io_started_at is None for row in skipped)

    # A second ticket isolates the ambiguous provider-returned-False path.
    second_ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        pregunta="Segundo ticket",
        nro_ticket="comment-100003",
        consulta_pin="332211",
        email_vecino="second-requester@example.com",
        telefono_vecino="+5492613444444",
    )
    db.session.add(second_ticket)
    db.session.commit()
    twilio_scope = _set_provider_config(app, monkeypatch, tenant)
    second_comment = ServicioTickets().crear_comentario(
        second_ticket.id,
        "municipio",
        _admin_comment_payload(),
    )
    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=False,
    ), patch(
        "services.email_service.enviar_sms_ticket_novedad",
        return_value=False,
    ), patch(
        "services.email_service.enviar_whatsapp_ticket_novedad",
        return_value=False,
    ), patch("socket_service.emit_new_chat_message"), patch(
        "services.tenant_twilio_messaging.Client"
    ) as twilio_client:
        twilio_client.return_value.messages.create.return_value = SimpleNamespace(
            sid=None
        )
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )
    unknown = DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == tenant.id,
        DomainEffectOutbox.aggregate_ref == str(second_comment.id),
        DomainEffectOutbox.handler_name != COMMENT_REALTIME_HANDLER,
    ).all()
    assert len(unknown) == 3
    assert all(row.status == DomainEffectOutbox.STATUS_UNKNOWN for row in unknown)
    assert all(row.io_started_at is not None for row in unknown)
    assert twilio_client.call_count == 2


def test_comment_email_preflight_rejects_invalid_requester_and_dispatch_before_io(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    monkeypatch.setitem(app.config, "EMAIL_NOTIFICATIONS_ENABLED", True)
    monkeypatch.setitem(app.config, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setitem(app.config, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setitem(app.config, "TWILIO_PHONE_NUMBER", "")
    monkeypatch.setitem(app.config, "TWILIO_WHATSAPP_NUMBER", "")

    owner = db.session.get(User, tenant.municipio_id)
    owner.email = ""
    tenant.dispatch_email = "dispatch-address-invalid"
    ticket.email_vecino = "requester-address-invalid"
    db.session.commit()

    with patch(
        "services.domain_effect_worker.enqueue_domain_effect_dispatch",
        return_value=False,
    ):
        admin_comment = ServicioTickets().crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(),
        )
        citizen_comment = ServicioTickets().crear_comentario(
            ticket.id,
            "municipio",
            {
                "comentario": "El problema continúa",
                "es_admin": False,
                "origen": "whatsapp",
            },
        )

    with patch(
        "services.email_service.validar_configuracion_smtp"
    ) as smtp_preflight, patch(
        "services.email_service.enviar_email_ticket_novedad"
    ) as requester_sender, patch(
        "services.email_service.enviar_email_ticket_admin"
    ) as admin_sender, patch(
        "socket_service.emit_new_chat_message"
    ):
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    requester_row = DomainEffectOutbox.query.filter_by(
        tenant_id=tenant.id,
        aggregate_ref=str(admin_comment.id),
        handler_name=COMMENT_REQUESTER_EMAIL_HANDLER,
    ).one()
    admin_row = DomainEffectOutbox.query.filter_by(
        tenant_id=tenant.id,
        aggregate_ref=str(citizen_comment.id),
        handler_name=COMMENT_ADMIN_EMAIL_HANDLER,
    ).one()
    assert requester_row.status == DomainEffectOutbox.STATUS_SKIPPED
    assert requester_row.io_started_at is None
    assert requester_row.result_json == {
        "reason_code": "requester_recipient_invalid"
    }
    assert admin_row.status == DomainEffectOutbox.STATUS_SKIPPED
    assert admin_row.io_started_at is None
    assert admin_row.result_json == {
        "reason_code": "admin_recipient_invalid"
    }
    smtp_preflight.assert_not_called()
    requester_sender.assert_not_called()
    admin_sender.assert_not_called()


def test_requester_profile_fallback_rejects_cross_tenant_user_before_io(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    _set_provider_config(app, monkeypatch, tenant)

    foreign_user = User(
        name="Foreign requester",
        email="foreign-requester@example.com",
        telefono="+5492613555555",
        rol="user",
    )
    foreign_user.set_password("test-password")
    db.session.add(foreign_user)
    db.session.flush()
    foreign_tenant = TenantProfile(
        slug="comment-effects-foreign",
        nombre="Comment Effects Foreign",
        tipo="pyme",
        pyme_id=foreign_user.id,
        is_active=True,
    )
    db.session.add(foreign_tenant)
    db.session.flush()
    foreign_user.tenant_id = foreign_tenant.id
    ticket.user_id = foreign_user.id
    ticket.email_vecino = None
    ticket.telefono_vecino = None
    db.session.commit()

    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(),
    )
    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ) as email_sender, patch(
        "services.email_service.enviar_sms_ticket_novedad",
        return_value=True,
    ) as sms_sender, patch(
        "services.email_service.enviar_whatsapp_ticket_novedad",
        return_value=True,
    ) as whatsapp_sender, patch(
        "socket_service.emit_new_chat_message"
    ) as socket_sender:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    requester_rows = DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == tenant.id,
        DomainEffectOutbox.aggregate_ref == str(comment.id),
        DomainEffectOutbox.handler_name.in_(
            [
                COMMENT_REQUESTER_EMAIL_HANDLER,
                COMMENT_REQUESTER_SMS_HANDLER,
                COMMENT_REQUESTER_WHATSAPP_HANDLER,
            ]
        ),
    ).all()
    assert summary.dead == 3
    assert len(requester_rows) == 3
    assert all(row.status == DomainEffectOutbox.STATUS_DEAD for row in requester_rows)
    assert all(row.io_started_at is None for row in requester_rows)
    assert all(
        row.last_error_code == "ticket_requester_tenant_binding_invalid"
        for row in requester_rows
    )
    email_sender.assert_not_called()
    sms_sender.assert_not_called()
    whatsapp_sender.assert_not_called()
    socket_sender.assert_called_once()


def test_two_canary_tenants_use_only_their_own_twilio_subaccount_and_sender(
    app,
    monkeypatch,
    municipal_comment_context,
):
    first_tenant, first_ticket, _ = municipal_comment_context
    second_owner = User(
        name="Second tenant owner",
        email="second-tenant-admin@example.com",
        rol="admin",
        tipo_chat="municipio",
    )
    second_owner.set_password("test-password")
    db.session.add(second_owner)
    db.session.flush()
    second_tenant = TenantProfile(
        slug="comment-effects-second-tenant",
        nombre="Second tenant",
        tipo="municipio",
        municipio_id=second_owner.id,
        is_active=True,
    )
    db.session.add(second_tenant)
    db.session.flush()
    second_owner.tenant_id = second_tenant.id
    second_ticket = MunicipioTicket(
        tenant_id=second_tenant.id,
        municipio_id=second_owner.id,
        pregunta="Segundo reclamo",
        nro_ticket="comment-tenant-200001",
        consulta_pin="778899",
        email_vecino="second-private@example.com",
        telefono_vecino="+5492613666666",
    )
    db.session.add(second_ticket)
    db.session.commit()

    _set_queue(app, monkeypatch, first_tenant.id)
    monkeypatch.setitem(
        app.config,
        "DOMAIN_EFFECT_OUTBOX_TENANT_IDS",
        f"{first_tenant.id},{second_tenant.id}",
    )
    first_scope = _set_provider_config(app, monkeypatch, first_tenant)
    second_scope = _set_provider_config(app, monkeypatch, second_tenant)
    second_service_sid = "MG" + ("2" * 32)
    second_scope["senders"]["whatsapp"].messaging_service_sid = second_service_sid
    db.session.commit()

    first_comment = ServicioTickets().crear_comentario(
        first_ticket.id,
        "municipio",
        _admin_comment_payload(),
    )
    second_comment = ServicioTickets().crear_comentario(
        second_ticket.id,
        "municipio",
        _admin_comment_payload(),
    )

    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ), patch("socket_service.emit_new_chat_message"), patch(
        "services.tenant_twilio_messaging.Client"
    ) as twilio_client:
        twilio_client.return_value.messages.create.side_effect = [
            SimpleNamespace(sid="SM-first-sms"),
            SimpleNamespace(sid="SM-first-wa"),
            SimpleNamespace(sid="SM-second-sms"),
            SimpleNamespace(sid="SM-second-wa"),
        ]
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=first_tenant.id,
            limit=10,
        )
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=second_tenant.id,
            limit=10,
        )

    assert first_comment is not None and second_comment is not None
    assert [item.args for item in twilio_client.call_args_list] == [
        (first_scope["account_sid"], first_scope["auth_token"]),
        (first_scope["account_sid"], first_scope["auth_token"]),
        (second_scope["account_sid"], second_scope["auth_token"]),
        (second_scope["account_sid"], second_scope["auth_token"]),
    ]
    provider_calls = twilio_client.return_value.messages.create.call_args_list
    assert provider_calls[0].kwargs["from_"] == first_scope["senders"]["sms"].phone_number
    assert provider_calls[1].kwargs["from_"] == (
        f"whatsapp:{first_scope['senders']['whatsapp'].phone_number}"
    )
    assert provider_calls[2].kwargs["from_"] == second_scope["senders"]["sms"].phone_number
    assert provider_calls[3].kwargs["messaging_service_sid"] == second_service_sid
    assert "from_" not in provider_calls[3].kwargs
    assert all(
        "global" not in str(item.args)
        for item in twilio_client.call_args_list
    )


def test_canary_without_provider_sender_never_uses_global_twilio(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    _set_provider_config(app, monkeypatch)
    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(),
    )

    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ), patch("socket_service.emit_new_chat_message"), patch(
        "services.tenant_twilio_messaging.Client"
    ) as twilio_client:
        dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    rows = {
        row.handler_name: row
        for row in DomainEffectOutbox.query.filter_by(
            tenant_id=tenant.id,
            aggregate_ref=str(comment.id),
        ).all()
    }
    assert rows[COMMENT_REQUESTER_SMS_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[COMMENT_REQUESTER_SMS_HANDLER].result_json == {
        "reason_code": "sms_tenant_sender_missing"
    }
    assert rows[COMMENT_REQUESTER_WHATSAPP_HANDLER].status == DomainEffectOutbox.STATUS_SKIPPED
    assert rows[COMMENT_REQUESTER_WHATSAPP_HANDLER].result_json == {
        "reason_code": "whatsapp_tenant_sender_missing"
    }
    assert rows[COMMENT_REQUESTER_SMS_HANDLER].io_started_at is None
    assert rows[COMMENT_REQUESTER_WHATSAPP_HANDLER].io_started_at is None
    twilio_client.assert_not_called()


def test_sender_change_after_staging_is_dead_before_twilio_io(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    twilio_scope = _set_provider_config(app, monkeypatch, tenant)
    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(),
    )
    target = DomainEffectOutbox.query.filter_by(
        tenant_id=tenant.id,
        aggregate_ref=str(comment.id),
        handler_name=COMMENT_REQUESTER_WHATSAPP_HANDLER,
    ).one()
    for row in DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == tenant.id,
        DomainEffectOutbox.aggregate_ref == str(comment.id),
        DomainEffectOutbox.id != target.id,
    ).all():
        row.available_at = row.available_at + timedelta(days=1)
    whatsapp_sender = twilio_scope["senders"]["whatsapp"]
    whatsapp_sender.phone_number = "+15005559999"
    whatsapp_sender.sender_id = "whatsapp:+15005559999"
    db.session.commit()

    with patch("services.tenant_twilio_messaging.Client") as twilio_client:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    db.session.refresh(target)
    assert summary.dead == 1
    assert target.status == DomainEffectOutbox.STATUS_DEAD
    assert target.io_started_at is None
    assert target.last_error_code == "tenant_twilio_sender_binding_mismatch"
    twilio_client.assert_not_called()


def test_status_callback_change_after_staging_is_dead_before_twilio_io(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    twilio_scope = _set_provider_config(app, monkeypatch, tenant)
    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        _admin_comment_payload(),
    )
    target = DomainEffectOutbox.query.filter_by(
        tenant_id=tenant.id,
        aggregate_ref=str(comment.id),
        handler_name=COMMENT_REQUESTER_WHATSAPP_HANDLER,
    ).one()
    for row in DomainEffectOutbox.query.filter(
        DomainEffectOutbox.tenant_id == tenant.id,
        DomainEffectOutbox.aggregate_ref == str(comment.id),
        DomainEffectOutbox.id != target.id,
    ).all():
        row.available_at = row.available_at + timedelta(days=1)
    twilio_scope["senders"]["whatsapp"].status_callback_url = (
        "https://other.example.test/twilio/whatsapp/status"
    )
    db.session.commit()

    with patch("services.tenant_twilio_messaging.Client") as twilio_client:
        summary = dispatch_domain_effects(
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            intent_secret=SECRET,
            tenant_id=tenant.id,
            limit=10,
        )

    db.session.refresh(target)
    assert summary.dead == 1
    assert target.status == DomainEffectOutbox.STATUS_DEAD
    assert target.io_started_at is None
    assert target.last_error_code == "tenant_twilio_sender_binding_mismatch"
    twilio_client.assert_not_called()


def test_comment_payload_validator_rejects_even_safe_extra_fields(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_queue(app, monkeypatch, tenant.id)
    comment = ServicioTickets().crear_comentario(
        ticket.id,
        "municipio",
        {"comentario": "Mensaje ciudadano", "es_admin": False},
    )
    row = DomainEffectOutbox.query.filter_by(
        tenant_id=tenant.id,
        handler_name=COMMENT_ADMIN_EMAIL_HANDLER,
    ).one()

    with pytest.raises(DomainEffectValidationError, match="ticket_effect_payload_invalid"):
        stage_domain_effect(
            tenant_id=tenant.id,
            aggregate_type=MUNICIPAL_COMMENT_AGGREGATE,
            aggregate_ref=str(comment.id),
            effect_type="ticket.comment.email.admin",
            handler_name=COMMENT_ADMIN_EMAIL_HANDLER,
            channel="email",
            recipient_ref="role:tenant.admins",
            effect_key=f"comment-validator:{comment.id}",
            intent_secret=SECRET,
            registry=TICKET_DOMAIN_EFFECT_REGISTRY,
            payload={**row.payload_json, "extra": "safe_marker"},
            session=db.session,
        )


def test_legacy_comment_keeps_direct_notification_and_socket_behavior(
    app,
    monkeypatch,
    municipal_comment_context,
):
    tenant, ticket, _ = municipal_comment_context
    _set_legacy(app, monkeypatch)

    with patch(
        "services.email_service.enviar_email_ticket_novedad",
        return_value=True,
    ) as email_sender, patch(
        "services.email_service.enviar_sms_ticket_novedad",
        return_value=True,
    ) as sms_sender, patch(
        "services.email_service.enviar_whatsapp_ticket_novedad",
        return_value=True,
    ) as whatsapp_sender, patch(
        "socket_service.emit_new_chat_message"
    ) as socket_sender:
        comment = ServicioTickets().crear_comentario(
            ticket.id,
            "municipio",
            _admin_comment_payload(),
        )

    assert comment is not None
    email_sender.assert_called_once()
    sms_sender.assert_called_once()
    whatsapp_sender.assert_called_once()
    socket_sender.assert_called_once()
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant.id).count() == 0
