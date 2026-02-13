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
    points_item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Puntos Test",
        precio_puntos=120,
        moneda="PTS",
        modalidad="canje",
    )
    db.session.add_all([product, donation, points_item])
    db.session.commit()

    return tenant, product, donation, points_item


def _build_cart(session, tenant_id, catalogo_item_id, cantidad=1):
    session.setdefault("carritos_pymes", {})[str(tenant_id)] = [
        {"catalogo_item_id": catalogo_item_id, "cantidad": cantidad}
    ]


@pytest.mark.usefixtures("client")
def test_anonymous_checkout_requires_contact(client, tenant_with_catalog):
    tenant, product, _, _ = tenant_with_catalog

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
    tenant, _, donation, _ = tenant_with_catalog

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


@pytest.mark.usefixtures("client")
def test_checkout_rejects_when_mercadopago_missing(client, tenant_with_catalog, monkeypatch):
    tenant, product, _, _ = tenant_with_catalog

    # Ensure no global token leaks
    monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)
    tenant.configuracion = {}
    db.session.commit()

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, product.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({"nombre": "Anon", "email": "anon@example.com"}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["estado"] == "pendiente_pago"
    assert data["mercadopago_ready"] is False
    assert "MercadoPago" in data["error"]




@pytest.mark.usefixtures("client")
def test_checkout_ignores_global_mercadopago_token_without_tenant_token(client, tenant_with_catalog, monkeypatch):
    tenant, product, _, _ = tenant_with_catalog

    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "global-token")
    tenant.configuracion = {}
    db.session.commit()

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, product.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({"nombre": "Anon", "email": "anon@example.com"}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 503
    data = resp.get_json()
    assert data["mercadopago_ready"] is False
    assert "no configurado" in data["error"].lower()

@pytest.mark.usefixtures("client")
def test_checkout_uses_tenant_token_for_payment(client, tenant_with_catalog, monkeypatch):
    tenant, product, _, _ = tenant_with_catalog

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: D401 - test double
        captured["auth"] = headers.get("Authorization") if headers else None

        class DummyResponse:
            ok = True

            def json(self):
                return {"init_point": "http://pay", "id": "pref-1"}

        return DummyResponse()

    tenant.configuracion = {"mercadopago_access_token": "tenant-token"}
    db.session.commit()
    monkeypatch.setattr("routes.checkout.requests.post", fake_post)

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, product.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({"nombre": "Anon", "email": "anon@example.com"}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["preference_id"] == "pref-1"
    assert captured["auth"] == "Bearer tenant-token"


@pytest.mark.usefixtures("client")
def test_points_checkout_rejects_when_insufficient_balance(client, tenant_with_catalog, monkeypatch):
    tenant, _, _, points_item = tenant_with_catalog

    class DummyUser:
        id = 999
        anon_id = None
        saldo_puntos = 10

    def fake_resolve(**kwargs):
        return tenant, DummyUser(), False

    monkeypatch.setattr("routes.checkout.resolve_tenant_and_user", lambda **kwargs: fake_resolve(**kwargs))

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, points_item.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 400
    data = resp.get_json()
    assert data["codigo"] == "SALDO_INSUFICIENTE"


@pytest.mark.usefixtures("client")
def test_money_checkout_ignores_client_demo_mode_flag(client, tenant_with_catalog, monkeypatch):
    tenant, product, _, _ = tenant_with_catalog

    tenant.configuracion = {"mercadopago_access_token": "test-token"}
    db.session.commit()

    called = {"mp": 0}

    class FakeResp:
        ok = True

        @staticmethod
        def json():
            return {"id": "pref_123", "init_point": "https://mp.test/pref_123"}

    def fake_post(*args, **kwargs):
        called["mp"] += 1
        return FakeResp()

    monkeypatch.setattr("routes.checkout.requests.post", fake_post)

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, product.id)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({"nombre": "Demo", "email": "demo@example.com", "demo_mode": True}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["estado"] == "pendiente_pago"
    assert data.get("demo_mode") is not True
    assert data["preference_id"] == "pref_123"
    assert called["mp"] == 1


@pytest.mark.usefixtures("client")
def test_points_checkout_confirms_and_deducts_balance(client, tenant_with_catalog, monkeypatch):
    from models import User

    tenant, _, _, points_item = tenant_with_catalog

    user = User(
        name="Puntos User",
        email="puntos-user@example.com",
        password_hash="hash",
        saldo_puntos=500,
        tenant_id=tenant.id,
    )
    db.session.add(user)
    db.session.commit()

    def fake_resolve(**kwargs):
        return tenant, user, False

    monkeypatch.setattr("routes.checkout.resolve_tenant_and_user", lambda **kwargs: fake_resolve(**kwargs))

    with client.session_transaction() as sess:
        _build_cart(sess, tenant.id, points_item.id, cantidad=2)

    resp = client.post(
        "/api/checkout/crear-preferencia",
        data=json.dumps({}),
        content_type="application/json",
        headers={"X-Tenant": tenant.slug},
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["tipo"] == "canje"
    assert data["total_puntos"] == 240
    assert data["estado"] == "confirmado"

    refreshed = User.query.get(user.id)
    assert refreshed.saldo_puntos == 260
