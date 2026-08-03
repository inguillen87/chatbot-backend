from datetime import datetime, timedelta

import jwt
import pytest

from database import db
from models import CatalogoItem, PedidoConversacional, TenantProfile, TenantTicket, User
from services.commerce_unified import serialize_unified_order
from services.whatsapp_assisted_intake import (
    create_whatsapp_assisted_intake,
    whatsapp_assisted_intake_eligible,
    whatsapp_assisted_intake_enabled,
)
from services.constants import CONTEXTO_MUNICIPIO


def _auth_headers(app, user: User, tenant_slug: str) -> dict:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant_slug}


def _owner_and_tenant(*, enabled: bool, tipo: str = "pyme") -> tuple[User, TenantProfile, User]:
    suffix = f"{tipo}-{enabled}"
    owner = User(
        name="Owner WA",
        email=f"owner-wa-{suffix}@chatboc.test",
        rol="admin",
        tipo_chat=tipo,
        tenant_slug=f"wa-intake-{suffix}",
    )
    owner.set_password("secret123")
    end_user = User(
        name="Cliente WhatsApp",
        email=f"cliente-wa-{enabled}@chatboc.test",
        rol="usuario",
        telefono="+5492613000000",
    )
    end_user.set_password("secret123")
    db.session.add_all([owner, end_user])
    db.session.flush()
    tenant = TenantProfile(
        slug=f"wa-intake-{suffix}",
        nombre="Ferreteria WhatsApp",
        tipo=tipo,
        pyme_id=owner.id if tipo == "pyme" else None,
        municipio_id=owner.id if tipo == "municipio" else None,
        configuracion={"whatsapp_assisted_intake_v1": enabled},
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.add(owner)
    db.session.commit()
    return owner, tenant, end_user


def test_whatsapp_assisted_intake_is_disabled_by_default(client):
    owner, tenant, end_user = _owner_and_tenant(enabled=False)

    assert whatsapp_assisted_intake_enabled(tenant, owner) is False

    with client.application.test_request_context("/whatsapp"):
        result = create_whatsapp_assisted_intake(
            tenant=tenant,
            owner_user=owner,
            end_user=end_user,
            session_id="whatsapp_1",
            from_number="+5492613000000",
            message_body="pedido por foto",
            uploaded_file_info={"mime_type": "image/jpeg", "name": "pedido.jpg", "url": "https://cdn.test/pedido.jpg"},
            media_bytes=b"fake-image",
        )

    assert result is None
    assert PedidoConversacional.query.count() == 0
    assert TenantTicket.query.count() == 0


def test_whatsapp_assisted_intake_creates_operational_ticket(client, monkeypatch):
    owner, tenant, end_user = _owner_and_tenant(enabled=True)
    assert whatsapp_assisted_intake_eligible(tenant, owner, {}) is True
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Clavos 2 pulgadas",
            sku="CLAVOS-2",
            precio="$ 1200",
            precio_monetario=1200,
            unidad="caja",
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "services.whatsapp_assisted_intake._extract_rows",
        lambda *_args, **_kwargs: [{"nombre": "Clavos 2 pulgadas", "cantidad": 3}, {"nombre": "Chapa acanalada", "cantidad": 2}],
    )
    monkeypatch.setattr("services.whatsapp_assisted_intake.track_marketplace_event", lambda *_args, **_kwargs: None)

    with client.application.test_request_context("/whatsapp"):
        result = create_whatsapp_assisted_intake(
            tenant=tenant,
            owner_user=owner,
            end_user=end_user,
            session_id="whatsapp_2",
            from_number="+5492613000000",
            message_body="te paso la nota de pedido",
            uploaded_file_info={
                "id": 88,
                "mime_type": "image/jpeg",
                "name": "pedido-ferreteria.jpg",
                "url": "https://cdn.test/pedido-ferreteria.jpg",
            },
            media_bytes=b"\xff\xd8\xff fake image",
            idempotency_key="media-1",
        )

    assert result["created"] is True
    assert result["contract_version"] == "marketplace.assisted_request.v1"
    assert result["source_contract_version"] == "whatsapp.assisted_intake.v1"
    assert result["match_summary"]["matched"] == 1
    assert result["match_summary"]["unmatched"] == 1

    pedido = db.session.get(PedidoConversacional, result["pedido_id"])
    ticket = db.session.get(TenantTicket, result["ticket_id"])
    assert pedido is not None
    assert ticket is not None
    assert pedido.metadata_payload["ticket_id"] == ticket.id
    assert pedido.metadata_payload["contract_version"] == "marketplace.assisted_request.v1"
    assert pedido.metadata_payload["source_contract_version"] == "whatsapp.assisted_intake.v1"
    assert pedido.metadata_payload["mode"] == "order_note_upload"
    assert pedido.metadata_payload["source_mode"] == "whatsapp_order_note_upload"
    assert pedido.metadata_payload["crm_order_draft"]["contract_version"] == "marketplace.crm_order_draft.v1"
    assert pedido.metadata_payload["crm_order_draft"]["reference"] == f"pedido:{pedido.id}"
    assert pedido.metadata_payload["crm_order_draft"]["lines"][1]["status"] == "needs_catalog_resolution"
    assert pedido.metadata_payload["crm_handoff"]["draft_order"] == pedido.metadata_payload["crm_order_draft"]
    assert pedido.metadata_payload["public_follow_up"]["tracking"]["code"] == f"pc-{pedido.id}"
    assert pedido.metadata_payload["operator_intake_summary"]["target_module"] == "orders"
    assert pedido.metadata_payload["linked_record"]["kind"] == "tenant_ticket"
    assert ticket.origen == "whatsapp"
    assert ticket.datos_extra["contract_version"] == "marketplace.commerce_intake_ticket.v1"
    assert ticket.datos_extra["assisted_request_contract_version"] == "marketplace.assisted_request.v1"
    assert ticket.datos_extra["pedido_conversacional_id"] == pedido.id
    assert ticket.datos_extra["source_contract_version"] == "whatsapp.assisted_intake.v1"
    assert "Chapa acanalada" in str(ticket.datos_extra)

    serialized = serialize_unified_order(pedido)
    assert serialized["assisted_request"]["contract_version"] == "marketplace.assisted_request.v1"
    assert serialized["assisted_request"]["source_contract_version"] == "whatsapp.assisted_intake.v1"
    assert serialized["assisted_request"]["source"]["channel"] == "whatsapp"
    assert serialized["assisted_request"]["crm_order_draft"]["reference"] == f"pedido:{pedido.id}"
    assert serialized["crm_review_card"]["contract_version"] == "marketplace.crm_review_card.v1"
    assert serialized["crm_review_card"]["contact_links"][0]["type"] == "whatsapp"


def test_whatsapp_assisted_intake_rejects_non_commerce_vertical_even_when_enabled(client):
    owner, tenant, end_user = _owner_and_tenant(enabled=True, tipo="municipio")

    assert whatsapp_assisted_intake_eligible(tenant, owner, {}) is False
    with client.application.test_request_context("/whatsapp"):
        result = create_whatsapp_assisted_intake(
            tenant=tenant,
            owner_user=owner,
            end_user=end_user,
            session_id="whatsapp-municipio",
            from_number="+5492613000000",
            message_body="foto de una luminaria caida",
            uploaded_file_info={
                "mime_type": "image/jpeg",
                "name": "luminaria.jpg",
                "url": "https://cdn.test/luminaria.jpg",
            },
            media_bytes=b"municipal-evidence",
        )

    assert result is None
    assert PedidoConversacional.query.count() == 0
    assert TenantTicket.query.count() == 0


@pytest.mark.parametrize(
    "conversation_context",
    [
        {CONTEXTO_MUNICIPIO: {"reclamo_flow_v2": {"state": "ESPERANDO_FOTO"}}},
        {"education_context": {"vertical": "educacion"}},
        {"human_chat_in_progress": True},
        {"room": "ticket_pyme_42"},
        {"active_ticket_followup": {"ticket_nro": "M-42", "until": 9999999999}},
    ],
    ids=["claim", "education", "live-chat", "live-room", "post-ticket"],
)
def test_whatsapp_assisted_intake_rejects_reserved_conversation_states(
    client,
    conversation_context,
):
    owner, tenant, _end_user = _owner_and_tenant(enabled=True)

    assert whatsapp_assisted_intake_eligible(
        tenant,
        owner,
        conversation_context,
    ) is False


def test_whatsapp_assisted_intake_exception_logs_exclude_content_and_provider_details(
    client,
    monkeypatch,
    caplog,
):
    owner, tenant, end_user = _owner_and_tenant(enabled=True)
    transcript = "Guillermo Persona Secreta dijo DNI 32877851 en Don Bosco 56"
    media_token = "https://media.twilio.test/private.ogg?token=media-secret"
    sql_provider_error = (
        "psycopg2 IntegrityError SQL params phone=5492613168608 "
        "provider_response=AC11111111111111111111111111111111"
    )

    def fail_extraction(*_args, **_kwargs):
        raise RuntimeError(f"{transcript} {media_token}")

    def fail_analytics(*_args, **_kwargs):
        raise RuntimeError(sql_provider_error)

    monkeypatch.setattr(
        "services.whatsapp_assisted_intake._extract_rows_from_text",
        fail_extraction,
    )
    monkeypatch.setattr(
        "services.whatsapp_assisted_intake.track_marketplace_event",
        fail_analytics,
    )
    caplog.set_level("WARNING", logger="services.whatsapp_assisted_intake")

    with client.application.test_request_context("/whatsapp"):
        result = create_whatsapp_assisted_intake(
            tenant=tenant,
            owner_user=owner,
            end_user=end_user,
            session_id="whatsapp_42_5492613168608\r\nFORGED_SESSION=1",
            from_number="5492613168608",
            message_body="pedido por audio transcripto",
            uploaded_file_info=None,
            media_bytes=None,
            idempotency_key="privacy-log-canary",
        )

    assert result is not None
    assert result["created"] is True
    rendered = caplog.text
    assert "RuntimeError" in rendered
    for private_value in (
        "Guillermo Persona Secreta",
        "32877851",
        "Don Bosco 56",
        "media.twilio.test",
        "media-secret",
        "5492613168608",
        "AC11111111111111111111111111111111",
        "psycopg2",
        "IntegrityError",
        "SQL params",
        "provider_response",
        "Traceback",
    ):
        assert private_value not in rendered


def test_whatsapp_assisted_intake_admin_catalog_resolution_updates_draft(client, monkeypatch):
    owner, tenant, end_user = _owner_and_tenant(enabled=True)
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Clavos 2 pulgadas",
            sku="CLAVOS-2",
            precio="$ 1200",
            precio_monetario=1200,
            unidad="caja",
        )
    )
    db.session.commit()

    monkeypatch.setattr(
        "services.whatsapp_assisted_intake._extract_rows",
        lambda *_args, **_kwargs: [{"nombre": "Clavos 2 pulgadas", "cantidad": 3}, {"nombre": "Chapa acanalada", "cantidad": 2}],
    )
    monkeypatch.setattr("services.whatsapp_assisted_intake.track_marketplace_event", lambda *_args, **_kwargs: None)

    with client.application.test_request_context("/whatsapp"):
        result = create_whatsapp_assisted_intake(
            tenant=tenant,
            owner_user=owner,
            end_user=end_user,
            session_id="whatsapp_3",
            from_number="+5492613000000",
            message_body="te paso la nota de pedido",
            uploaded_file_info={
                "id": 89,
                "mime_type": "image/jpeg",
                "name": "pedido-ferreteria.jpg",
                "url": "https://cdn.test/pedido-ferreteria.jpg",
            },
            media_bytes=b"\xff\xd8\xff fake image 2",
            idempotency_key="media-2",
        )

    chapa = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Chapa acanalada",
        sku="CHAPA-AC",
        precio="$ 9800",
        precio_monetario=9800,
        unidad="unidad",
    )
    db.session.add(chapa)
    db.session.commit()

    response = client.patch(
        f"/api/admin/tenants/{tenant.slug}/orders/conversational:{result['pedido_id']}",
        json={"catalog_resolutions": [{"line_id": "line-2", "catalog_item_id": chapa.id}]},
        headers=_auth_headers(client.application, owner, tenant.slug),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["assisted_request"]["match_summary"]["matched"] == 2
    assert payload["assisted_request"]["match_summary"]["unmatched"] == 0
    resolved_line = payload["assisted_request"]["crm_order_draft"]["lines"][1]
    assert resolved_line["status"] == "catalog_matched"
    assert resolved_line["catalog_item_id"] == chapa.id
