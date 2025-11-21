import json

import pytest

from app import db
from models import CatalogoItem, TenantProfile


@pytest.fixture
def tenant_with_catalog(init_database):
    from models import User

    owner = User.query.get(1)
    tenant = TenantProfile(slug="demo", municipio_id=owner.id, nombre="Demo Tenant", tipo="municipio")
    db.session.add(tenant)
    db.session.commit()

    product = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto Test",
        precio_monetario=100,
        moneda="ARS",
        modalidad="venta",
    )
    donation = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Donacion Test",
        precio_monetario=0,
        moneda="ARS",
        modalidad="donacion",
    )
    db.session.add_all([product, donation])
    db.session.commit()

    return tenant, product, donation


def _build_cart(session, tenant_id, catalogo_item_id, cantidad=1):
    session.setdefault("carritos_pymes", {})[str(tenant_id)] = [
        {"catalogo_item_id": catalogo_item_id, "cantidad": cantidad}
    ]


@pytest.mark.usefixtures("client")
def test_anonymous_checkout_requires_contact(client, tenant_with_catalog):
    tenant, product, _ = tenant_with_catalog

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, product.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 400
    data = resp.get_json()
    assert data.get("contacto_requerido") is True
    assert "contacto" in data.get("error", "").lower()


@pytest.mark.usefixtures("client")
def test_donation_checkout_confirms_without_payment(client, tenant_with_catalog):
    tenant, _, donation = tenant_with_catalog

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, donation.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({"nombre": "Anon Donor", "email": "donor@example.com"}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_monetario"] == 0
    assert data["total_puntos"] == 0
    assert data["tipo"] == "donacion"
    assert data["estado"] == "confirmado"

    from models import PedidoConversacional

    pedido = PedidoConversacional.query.get(data["pedido_id"])
    assert pedido is not None
    assert pedido.tipo == "donacion"
    assert pedido.estado == "confirmado"
