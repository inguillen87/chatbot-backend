"""The persisted claim credential must own every receipt, including replays."""

from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from models import MunicipioTicket, TenantProfile, TicketDomainEffectReceipt, User, db
from services.actions.municipio_actions import CrearReclamoActionHandler
from services.constants import CONTEXTO_MUNICIPIO


@pytest.fixture()
def receipt_env(client):
    owner = User(
        id=83001,
        name="Fixture Municipality",
        email="receipt-owner@example.test",
        password_hash="fixture-hash",
        rol="admin",
        tipo_chat="municipio",
        municipio_id=83001,
        tenant_slug="receipt-pin-fixture",
    )
    tenant = TenantProfile(
        slug="receipt-pin-fixture",
        nombre="Fixture Municipality",
        tipo="municipio",
        municipio_id=owner.id,
        is_active=True,
    )
    db.session.add_all([owner, tenant])
    db.session.commit()
    with ExitStack() as stack:
        for name in ("validar_email", "validar_telefono", "direccion_es_valida"):
            stack.enter_context(
                patch("services.actions.municipio_actions." + name, return_value=True)
            )
        for target, value in (
            ("services.actions.municipio_actions.parse_direccion", {"localidad": "Fixture City"}),
            ("services.actions.municipio_actions.formatear_telefono_e164", "+15550102030"),
            ("services.actions.municipio_actions.cargar_configuracion_municipio", {}),
            ("services.actions.municipio_actions._persist_municipio_ticket_ai_enrichment", None),
            ("services.actions.municipio_actions.promo_service.build_ticket_promo_section", None),
            ("socket_service.emit_new_ticket", None),
            ("services.ticket_service.servicio_tickets._notificar_ticket_por_email", None),
            ("services.ticket_service.enviar_ticket_a_sigem", True),
            ("services.ticket_domain_effects.stage_ticket_created_effects", False),
        ):
            stack.enter_context(patch(target, return_value=value))
        stack.enter_context(
            patch("services.ticket_service.servicio_tickets.auto_assign_enabled", False)
        )
        yield client, owner, tenant


def _context(owner, tenant):
    return {
        "user_obj": owner,
        "tenant_profile": tenant,
        "tenant_id": tenant.id,
        "viewer_user_obj": None,
        "anon_id": "fixture-replay-person",
        "channel": "whatsapp",
        "durable_turn_id": "fixture-pin-replay-turn-0001",
        "municipio_config_actual": {"base_chat_url": "https://fixture.example.test/chat"},
        "chat_db_context_data": {CONTEXTO_MUNICIPIO: {}},
    }


def _action(candidate_pin, **overrides):
    return {
        "categoria": "Arbolado",
        "descripcion": "Hay una rama caída sobre la vereda.",
        "ubicacion": "Calle Fixture 100, Ciudad Fixture",
        "usuario": "Persona Fixture",
        "dni": "99999999",
        "email": "fixture-person@example.test",
        "telefono": "+15550102030",
        "pin": candidate_pin,
        **overrides,
    }


def _create(receipt_env, candidate_pin, **overrides):
    _, owner, tenant = receipt_env
    return CrearReclamoActionHandler(_context(owner, tenant)).execute(
        _action(candidate_pin, **overrides)
    )


def _track(client, ticket, pin):
    return client.get(
        "/api/public/tracking/experience",
        query_string={"kind": "claim", "code": ticket.nro_ticket},
        headers={"X-Tracking-Pin": pin},
    )


def test_new_claim_receipt_uses_persisted_pin(receipt_env):
    client, _, tenant = receipt_env
    result = _create(receipt_env, "411111")
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()

    assert result["success"] is True
    assert result["data"]["consulta_pin"] == ticket.consulta_pin == "411111"
    assert _track(client, ticket, result["data"]["consulta_pin"]).status_code == 200


def test_receipt_preserves_digit_characters_accepted_by_existing_pin_validation(receipt_env):
    _, _, tenant = receipt_env
    accepted_pin = "١٢٣٤٥٦"
    result = _create(receipt_env, accepted_pin)
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()

    assert result["success"] is True
    assert result["data"]["consulta_pin"] == ticket.consulta_pin == accepted_pin
    assert parse_qs(urlsplit(result["tracking_url"]).fragment) == {"pin": [accepted_pin]}


def test_durable_replay_receipt_uses_original_pin_on_all_surfaces(receipt_env):
    client, _, tenant = receipt_env
    first = _create(receipt_env, "422222")
    replay = _create(receipt_env, "433333")
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()

    assert first["success"] is replay["success"] is True
    assert first["data"]["ticket_id"] == replay["data"]["ticket_id"] == ticket.id
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=tenant.id).count() == 1
    assert ticket.consulta_pin == first["data"]["consulta_pin"] == "422222"
    assert replay["data"]["consulta_pin"] == ticket.consulta_pin
    assert replay["contexto_actualizado"]["latest_ticket_pin"] == ticket.consulta_pin
    assert (
        replay["contexto_actualizado"]["active_ticket_followup"]["consulta_pin"]
        == ticket.consulta_pin
    )
    fragment = parse_qs(urlsplit(replay["tracking_url"]).fragment)
    assert fragment == {"pin": [ticket.consulta_pin]}
    assert _track(client, ticket, replay["data"]["consulta_pin"]).status_code == 200
    assert _track(client, ticket, "433333").status_code == 404


def test_confirmation_replay_keeps_existing_pin_without_duplicate(receipt_env):
    client, _, tenant = receipt_env
    first = _create(receipt_env, "444444", claim_confirmation_id="fixture-confirmation-0001")
    replay = _create(receipt_env, "455555", claim_confirmation_id="fixture-confirmation-0001")
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()

    assert first["success"] is replay["success"] is True
    assert replay["data"]["deduplicated"] is True
    assert replay["data"]["ticket_id"] == first["data"]["ticket_id"] == ticket.id
    assert replay["data"]["consulta_pin"] == ticket.consulta_pin == "444444"
    assert _track(client, ticket, replay["data"]["consulta_pin"]).status_code == 200
    assert _track(client, ticket, "455555").status_code == 404


@pytest.mark.parametrize("returned_pin", [None, "", "123", "invalid", True])
def test_invalid_persisted_pin_fails_closed_instead_of_using_candidate(receipt_env, returned_pin):
    _, _, tenant = receipt_env
    first = _create(receipt_env, "466666")
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()
    service_result = {
        "id": ticket.id,
        "nro_ticket": ticket.nro_ticket,
        "consulta_pin": returned_pin,
    }
    with patch(
        "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
        return_value=service_result,
    ):
        rejected = _create(receipt_env, "477777")

    assert first["success"] is True
    assert rejected["success"] is False
    assert "tracking_url" not in rejected
    assert "consulta_pin" not in rejected.get("data", {})
    assert ticket.consulta_pin == "466666"
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1


def test_missing_persisted_pin_fails_closed_instead_of_using_candidate(receipt_env):
    _, _, tenant = receipt_env
    first = _create(receipt_env, "488888")
    ticket = MunicipioTicket.query.filter_by(tenant_id=tenant.id).one()
    with patch(
        "services.actions.municipio_actions.servicio_tickets.crear_nuevo_ticket",
        return_value={"id": ticket.id, "nro_ticket": ticket.nro_ticket},
    ):
        rejected = _create(receipt_env, "499999")

    assert first["success"] is True
    assert rejected["success"] is False
    assert "tracking_url" not in rejected
    assert "consulta_pin" not in rejected.get("data", {})
    assert ticket.consulta_pin == "488888"
    assert MunicipioTicket.query.filter_by(tenant_id=tenant.id).count() == 1
