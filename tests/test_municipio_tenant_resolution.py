from unittest.mock import MagicMock, patch

from models import ChatSessionContext, TenantProfile, User, db
from services.actions.municipio_actions import _resolve_municipio_tenant_ids
from services.constants import CONTEXTO_MUNICIPIO
from services.municipio_responder import (
    ReclamoState,
    _resolve_authoritative_municipio_tenant,
    responder_municipio,
)
from services.tenant_resolver import _tenant_by_number


def _create_municipal_tenants(owner_id: int):
    first = TenantProfile(
        slug="municipio-scope-first",
        nombre="Municipio Scope First",
        tipo="municipio",
        municipio_id=owner_id,
        is_active=True,
    )
    second = TenantProfile(
        slug="municipio-scope-second",
        nombre="Municipio Scope Second",
        tipo="municipio",
        municipio_id=owner_id,
        is_active=True,
    )
    db.session.add_all([first, second])
    db.session.commit()
    return first, second


def test_junin_destination_uses_registered_sender_not_profile_or_sandbox(
    client,
    owner_user,
):
    junin_sender = "+17432643718"
    profile_contact = "+5492615550101"
    twilio_sandbox = "+14155238886"
    owner_user.telefono = profile_contact
    tenant = TenantProfile(
        slug="junin-sender-scope",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        whatsapp_sender_id=f"whatsapp:{junin_sender}",
        is_active=True,
    )
    db.session.add(tenant)
    db.session.commit()

    assert _tenant_by_number(f"whatsapp:{junin_sender}").id == tenant.id
    assert _tenant_by_number(profile_contact) is None
    assert _tenant_by_number(f"whatsapp:{twilio_sandbox}") is None


def test_session_tenant_wins_when_owner_has_multiple_profiles(client, owner_user):
    first, expected = _create_municipal_tenants(owner_user.id)
    session = ChatSessionContext(
        chat_session_id="municipio-tenant-scope-session",
        user_id=owner_user.id,
        tenant_id=expected.id,
        context_data={},
    )
    db.session.add(session)
    db.session.commit()

    resolved = _resolve_authoritative_municipio_tenant(owner_user, session)

    assert resolved.id == expected.id
    assert resolved.id != first.id


def test_explicit_tenant_wins_and_is_validated_against_owner(client, owner_user):
    _, expected = _create_municipal_tenants(owner_user.id)

    resolved = _resolve_authoritative_municipio_tenant(
        owner_user,
        explicit_profile=expected,
        explicit_tenant_id=expected.id,
    )

    assert resolved.id == expected.id


def test_ambiguous_owner_lookup_fails_closed(client, owner_user):
    _create_municipal_tenants(owner_user.id)

    assert _resolve_authoritative_municipio_tenant(owner_user) is None


def test_ticket_action_uses_same_session_tenant_scope(client, owner_user):
    first, expected = _create_municipal_tenants(owner_user.id)
    session = ChatSessionContext(
        chat_session_id="municipio-action-tenant-scope",
        user_id=owner_user.id,
        tenant_id=expected.id,
        context_data={},
    )
    db.session.add(session)
    db.session.commit()

    tenant_id, municipio_id = _resolve_municipio_tenant_ids(
        owner_user,
        {
            "chat_db_context_obj": session,
            "tenant_profile": expected,
            "tenant_id": expected.id,
        },
    )

    assert tenant_id == expected.id
    assert tenant_id != first.id
    assert municipio_id == owner_user.id


def test_conflicting_ticket_action_tenant_scopes_fail_closed(client, owner_user):
    first, second = _create_municipal_tenants(owner_user.id)
    session = ChatSessionContext(
        chat_session_id="municipio-action-tenant-conflict",
        user_id=owner_user.id,
        tenant_id=second.id,
        context_data={},
    )
    db.session.add(session)
    db.session.commit()

    assert _resolve_municipio_tenant_ids(
        owner_user,
        {
            "chat_db_context_obj": session,
            "tenant_profile": first,
            "tenant_id": first.id,
        },
    ) == (None, None)


def test_non_scalar_mock_owner_ids_fail_closed_before_query(client):
    owner = MagicMock(spec=User)

    assert _resolve_municipio_tenant_ids(owner, {}) == (None, None)


def test_responder_preserves_authoritative_tenant_until_claim_action(client, owner_user):
    _, expected = _create_municipal_tenants(owner_user.id)
    session = ChatSessionContext(
        chat_session_id="municipio-responder-tenant-scope",
        user_id=owner_user.id,
        tenant_id=expected.id,
        anon_id="tenant-scope-citizen",
        context_data={
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "EN_FLUJO_RECLAMO",
                "reclamo_flow_v2": {
                    "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
                    "confirmation_id": "tenant-scope-confirmation",
                    "datos_reclamo": {
                        "categoria": "Arbolado",
                        "descripcion": "Rama caida sobre la vereda",
                        "direccion": "Don Bosco 56",
                        "nombre": "Vecino de prueba",
                        "telefono": "+5492615550000",
                        "email": "vecino@example.test",
                    },
                },
            }
        },
    )
    db.session.add(session)
    db.session.commit()

    action_result = {
        "success": True,
        "message_body": "Reclamo recibido.",
        "message_type": "text",
        "data": {
            "ticket_id": 901,
            "nro_ticket": "M-901",
            "consulta_pin": "123456",
            "tracking_url": "https://chatboc.test/tracking/claim/M-901",
        },
        "contexto_actualizado": {
            "latest_ticket_id": 901,
            "latest_ticket_nro": "M-901",
        },
    }

    with patch("services.municipio_responder.CrearReclamoActionHandler") as handler_cls:
        handler_cls.return_value.execute.return_value = action_result
        responder_municipio(
            pregunta_original={
                "pregunta": "confirmar",
                "action": "reclamo_confirmar_si",
            },
            owner_user=owner_user,
            viewer_user=None,
            rubro_obj=owner_user.rubro,
            chat_db_context=session,
            anon_id="tenant-scope-citizen",
            channel="whatsapp",
            chat_session_uuid=session.chat_session_id,
            tenant_profile=expected,
            tenant_id=expected.id,
        )

    action_context = handler_cls.call_args.args[0]
    assert action_context["tenant_id"] == expected.id
    assert action_context["tenant_profile"].id == expected.id
    assert action_context["chat_db_context_obj"].tenant_id == expected.id
