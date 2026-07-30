from __future__ import annotations

from unittest.mock import Mock, patch

import pytest
from sqlalchemy.exc import IntegrityError

from models import (
    MunicipioTicket,
    TenantProfile,
    TicketComentario,
    TicketDomainEffectReceipt,
    db,
)
from services.ticket_service import (
    ServicioTickets,
    TicketIdempotencyConflict,
    TicketIdempotencyValidationError,
    build_whatsapp_ticket_effect_key,
    canonical_ticket_payload_hash,
)


@pytest.fixture
def municipal_tenants(init_database, owner_user):
    tenant_a = TenantProfile(
        slug="ticket-effects-a",
        nombre="Ticket Effects A",
        tipo="municipio",
        municipio_id=owner_user.id,
    )
    tenant_b = TenantProfile(
        slug="ticket-effects-b",
        nombre="Ticket Effects B",
        tipo="municipio",
        municipio_id=owner_user.id,
    )
    db.session.add_all([tenant_a, tenant_b])
    db.session.commit()
    return tenant_a, tenant_b


def _ticket_payload(tenant: TenantProfile, owner_user, **overrides):
    payload = {
        "tenant_id": tenant.id,
        "municipio_id": owner_user.id,
        "asunto": "Reclamo (LLM): Luminaria",
        "categoria": "Luminarias",
        "pregunta": "Hay un poste sin luz",
        "detalles": "Poste apagado hace dos noches",
        "direccion": "San Martín 123",
        "nombre_vecino": "Persona Privada",
        "telefono_vecino": "+5492613000000",
        "email_vecino": "persona@example.com",
        "consulta_pin": "111111",
        "estado": "nuevo",
    }
    payload.update(overrides)
    return payload


def test_ticket_replay_returns_original_resource_without_duplicate_side_effects(
    municipal_tenants,
    owner_user,
):
    tenant, _ = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    email_notifier = Mock()
    service._notificar_ticket_por_email = email_notifier
    key = "whatsapp:1:turn-ticket-replay:municipio_claim_create"

    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True) as sigem:
        first = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant, owner_user),
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )
        replay = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant, owner_user, consulta_pin="999999"),
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )

    assert replay == first
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1
    assert TicketDomainEffectReceipt.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=key,
    ).count() == 1
    sigem.assert_called_once()
    email_notifier.assert_called_once()

    receipt = TicketDomainEffectReceipt.query.filter_by(
        tenant_id=tenant.id,
        idempotency_key=key,
    ).one()
    assert receipt.resource_id == first["id"]
    assert receipt.result_json == {
        "id": first["id"],
        "nro_ticket": first["nro_ticket"],
        "tipo_ticket": "municipio",
    }
    serialized_receipt = str(receipt.result_json)
    assert "Persona Privada" not in serialized_receipt
    assert "persona@example.com" not in serialized_receipt
    assert first["consulta_pin"] not in serialized_receipt


def test_ticket_key_reuse_with_different_payload_fails_closed(
    municipal_tenants,
    owner_user,
):
    tenant, _ = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    key = "external:municipio:conflict-0001"

    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True):
        first = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant, owner_user),
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )
        with pytest.raises(TicketIdempotencyConflict):
            service.crear_nuevo_ticket(
                "municipio",
                _ticket_payload(
                    tenant,
                    owner_user,
                    detalles="Ahora intenta crear otro reclamo",
                ),
                idempotency_key=key,
                idempotency_tenant_id=tenant.id,
            )

    assert first is not None
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=tenant.id).count() == 1


def test_concurrent_ticket_receipt_loser_replays_the_committed_winner(
    municipal_tenants,
    owner_user,
):
    tenant, _ = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    email_notifier = Mock()
    service._notificar_ticket_por_email = email_notifier
    key = "external:municipio:race-0001"
    payload = _ticket_payload(tenant, owner_user)

    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True) as sigem:
        winner = service.crear_nuevo_ticket(
            "municipio",
            payload,
            idempotency_key=key,
            idempotency_tenant_id=tenant.id,
        )
        receipt = TicketDomainEffectReceipt.query.filter_by(
            tenant_id=tenant.id,
            idempotency_key=key,
        ).one()

        # Model the window where two workers both missed the preflight lookup:
        # the loser reaches commit after the winner's unique receipt exists.
        with patch.object(
            service,
            "_find_effect_receipt",
            side_effect=[None, receipt],
        ), patch.object(
            db.session,
            "commit",
            side_effect=IntegrityError(
                "INSERT ticket_domain_effect_receipt",
                {},
                Exception("unique tenant/key"),
            ),
        ):
            replay = service.crear_nuevo_ticket(
                "municipio",
                payload,
                idempotency_key=key,
                idempotency_tenant_id=tenant.id,
            )

    assert replay == winner
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=tenant.id).count() == 1
    sigem.assert_called_once()
    email_notifier.assert_called_once()


def test_same_key_is_isolated_by_tenant(municipal_tenants, owner_user):
    tenant_a, tenant_b = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    key = "external:shared:ticket-key-0001"

    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True):
        ticket_a = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant_a, owner_user),
            idempotency_key=key,
            idempotency_tenant_id=tenant_a.id,
        )
        ticket_b = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant_b, owner_user),
            idempotency_key=key,
            idempotency_tenant_id=tenant_b.id,
        )

    assert ticket_a["id"] != ticket_b["id"]
    assert MunicipioTicket.query.count() == 2
    assert TicketDomainEffectReceipt.query.filter_by(idempotency_key=key).count() == 2


def test_comment_replay_does_not_duplicate_and_changed_text_conflicts(
    municipal_tenants,
    owner_user,
):
    tenant, _ = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True):
        ticket = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant, owner_user),
        )

    comment_key = "whatsapp:1:turn-comment-replay:live_chat_comment"
    comment_payload = {
        "comentario": "La luminaria sigue apagada",
        "origen": "whatsapp",
        "es_admin": False,
        "estado_ticket": "nuevo",
        "emit_notifications": False,
        "emit_socket": False,
    }
    first = service.crear_comentario(
        ticket["id"],
        "municipio",
        comment_payload,
        idempotency_key=comment_key,
        idempotency_tenant_id=tenant.id,
    )
    replay = service.crear_comentario(
        ticket["id"],
        "municipio",
        dict(comment_payload),
        idempotency_key=comment_key,
        idempotency_tenant_id=tenant.id,
    )

    assert replay.id == first.id
    assert first.estado_ticket == "nuevo"
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket["id"]).count() == 1

    for changed_payload in (
        {**comment_payload, "emit_notifications": True},
        {**comment_payload, "emit_socket": True},
        {**comment_payload, "comentario": "Contenido diferente"},
    ):
        with pytest.raises(TicketIdempotencyConflict):
            service.crear_comentario(
                ticket["id"],
                "municipio",
                changed_payload,
                idempotency_key=comment_key,
                idempotency_tenant_id=tenant.id,
            )
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket["id"]).count() == 1


def test_comment_replay_normalizes_omitted_effect_flags_to_true(
    municipal_tenants,
    owner_user,
):
    tenant, _ = municipal_tenants
    service = ServicioTickets()
    service.auto_assign_enabled = False
    service._notificar_ticket_por_email = Mock()
    with patch("services.ticket_service.enviar_ticket_a_sigem", return_value=True):
        ticket = service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant, owner_user),
        )

    comment_key = "whatsapp:1:turn-comment-default-flags:live-comment"
    payload = {
        "comentario": "Comentario con efectos por defecto",
        "origen": "whatsapp",
        "es_admin": False,
    }
    first = service.crear_comentario(
        ticket["id"],
        "municipio",
        payload,
        idempotency_key=comment_key,
        idempotency_tenant_id=tenant.id,
    )
    replay = service.crear_comentario(
        ticket["id"],
        "municipio",
        {**payload, "emit_notifications": True, "emit_socket": True},
        idempotency_key=comment_key,
        idempotency_tenant_id=tenant.id,
    )

    assert replay.id == first.id
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket["id"]).count() == 1

    with pytest.raises(TicketIdempotencyConflict):
        service.crear_comentario(
            ticket["id"],
            "municipio",
            {**payload, "emit_notifications": False, "emit_socket": True},
            idempotency_key=comment_key,
            idempotency_tenant_id=tenant.id,
        )
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket["id"]).count() == 1


def test_ticket_idempotency_requires_matching_tenant_scope(
    municipal_tenants,
    owner_user,
):
    tenant_a, tenant_b = municipal_tenants
    service = ServicioTickets()

    with pytest.raises(TicketIdempotencyValidationError):
        service.crear_nuevo_ticket(
            "municipio",
            _ticket_payload(tenant_a, owner_user),
            idempotency_key="external:tenant:mismatch-0001",
            idempotency_tenant_id=tenant_b.id,
        )
    assert MunicipioTicket.query.count() == 0
    assert TicketDomainEffectReceipt.query.count() == 0


def test_canonical_hash_and_whatsapp_key_are_stable_and_bounded():
    first = canonical_ticket_payload_hash(
        "ticket.create.municipio",
        {"b": [2, 1], "a": {"z": "á", "x": True}},
    )
    second = canonical_ticket_payload_hash(
        "ticket.create.municipio",
        {"a": {"x": True, "z": "á"}, "b": [2, 1]},
    )
    assert first == second
    assert len(first) == 64

    key = build_whatsapp_ticket_effect_key(
        42,
        "00000000-0000-0000-0000-000000000042",
        "municipio_claim_create",
    )
    assert key == (
        "whatsapp:42:00000000-0000-0000-0000-000000000042:"
        "municipio_claim_create"
    )
    assert len(key) <= 191

    with pytest.raises(TicketIdempotencyValidationError):
        build_whatsapp_ticket_effect_key(0, "valid-turn-0001", "claim")
    with pytest.raises(TicketIdempotencyValidationError):
        build_whatsapp_ticket_effect_key(1, "unsafe turn", "claim")


def test_voice_claim_uses_call_scoped_key_but_does_not_reuse_it_for_comments():
    from services.actions.municipio_actions import _durable_ticket_effect_kwargs

    voice_key = f"voice:{'a' * 64}"
    context = {
        "channel": "voice",
        "tenant_id": 77,
        "idempotency_key": voice_key,
    }
    assert _durable_ticket_effect_kwargs(
        context,
        "municipio_claim_create",
    ) == {
        "idempotency_key": voice_key,
        "idempotency_tenant_id": 77,
    }
    assert _durable_ticket_effect_kwargs(
        context,
        "municipio_live_chat_initial_comment",
    ) == {}

    with pytest.raises(TicketIdempotencyValidationError):
        _durable_ticket_effect_kwargs(
            {**context, "idempotency_key": "voice:unsafe"},
            "municipio_claim_create",
        )
