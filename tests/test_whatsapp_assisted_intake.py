from database import db
from models import CatalogoItem, PedidoConversacional, TenantProfile, TenantTicket, User
from services.whatsapp_assisted_intake import (
    create_whatsapp_assisted_intake,
    whatsapp_assisted_intake_enabled,
)


def _owner_and_tenant(*, enabled: bool) -> tuple[User, TenantProfile, User]:
    owner = User(
        name="Owner WA",
        email=f"owner-wa-{enabled}@chatboc.test",
        rol="admin",
        tenant_slug=f"wa-intake-{enabled}",
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
        slug=f"wa-intake-{enabled}",
        nombre="Ferreteria WhatsApp",
        tipo="pyme",
        pyme_id=owner.id,
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
    assert result["contract_version"] == "whatsapp.assisted_intake.v1"
    assert result["match_summary"]["matched"] == 1
    assert result["match_summary"]["unmatched"] == 1

    pedido = db.session.get(PedidoConversacional, result["pedido_id"])
    ticket = db.session.get(TenantTicket, result["ticket_id"])
    assert pedido is not None
    assert ticket is not None
    assert pedido.metadata_payload["ticket_id"] == ticket.id
    assert ticket.origen == "whatsapp"
    assert ticket.datos_extra["contract_version"] == "whatsapp.assisted_intake.v1"
    assert ticket.datos_extra["assisted_request_contract_version"] == "marketplace.assisted_request.v1"
    assert "Chapa acanalada" in str(ticket.datos_extra)
