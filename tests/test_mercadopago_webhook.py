import json
import os

import pytest

from app import db
from models import PedidoConversacional, TenantProfile, User


@pytest.fixture
def tenant_with_pedido(init_database):
    owner = User.query.get(1)
    tenant = TenantProfile(
        slug="mp-demo", municipio_id=owner.id, nombre="MP Demo", tipo="municipio"
    )
    tenant.configuracion = {"mercadopago_access_token": "tenant-token"}
    db.session.add(tenant)
    db.session.commit()

    pedido = PedidoConversacional(
        tenant_id=tenant.id,
        user_id=owner.id,
        monto_monetario=100,
        tipo="compra",
        estado="pendiente_pago",
    )
    db.session.add(pedido)
    db.session.commit()
    return tenant, pedido


@pytest.mark.usefixtures("client")
def test_webhook_uses_tenant_token_and_emits_notification(
    client, tenant_with_pedido, monkeypatch
):
    tenant, pedido = tenant_with_pedido

    captured = {}

    def fake_get(url, headers=None, **_kwargs):  # noqa: D401 - test double
        auth = (headers or {}).get("Authorization")
        captured.setdefault("auth_calls", []).append(auth)

        class DummyResp:
            status_code = 200

            def json(self):
                return {"external_reference": str(pedido.id), "status": "approved"}

            @property
            def ok(self):
                return True

        return DummyResp()

    emitted = []

    def fake_emit(event, data, **_kwargs):
        emitted.append((event, data))

    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "fallback-token")
    monkeypatch.setattr("routes.mercadopago_webhook.requests.get", fake_get)
    monkeypatch.setattr("routes.mercadopago_webhook.socketio.emit", fake_emit)

    resp = client.post(
        "/mercadopago_webhook",
        data=json.dumps({"type": "payment", "data": {"id": "pay-1"}}),
        content_type="application/json",
    )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["estado"] == "pagado"

    # First call uses global token, second call retries with tenant token
    assert "Bearer fallback-token" in captured.get("auth_calls", [])
    assert "Bearer tenant-token" in captured.get("auth_calls", [])

    pedido_refreshed = PedidoConversacional.query.get(pedido.id)
    assert pedido_refreshed.estado == "pagado"

    assert emitted
    event, payload = emitted[0]
    assert event == "payment_update"
    assert payload == {"contract_version": "collections.invalidated.v1", "resource": "payments", "reason": "collection_changed", "refetch": True}


@pytest.mark.usefixtures("client")
def test_webhook_fails_without_global_token(client, tenant_with_pedido, monkeypatch):
    _, pedido = tenant_with_pedido

    monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)

    resp = client.post(
        "/mercadopago_webhook",
        data=json.dumps({"type": "payment", "data": {"id": str(pedido.id)}}),
        content_type="application/json",
    )

    assert resp.status_code == 503
    data = resp.get_json()
    assert "MercadoPago" in data.get("error", "")



@pytest.mark.usefixtures("client")
def test_webhook_works_with_payload_tenant_token_without_global(client, tenant_with_pedido, monkeypatch):
    tenant, pedido = tenant_with_pedido

    captured = {}

    def fake_get(url, headers=None, **_kwargs):
        captured.setdefault("auth_calls", []).append((headers or {}).get("Authorization"))

        class DummyResp:
            status_code = 200

            def json(self):
                return {"external_reference": str(pedido.id), "status": "approved"}

            @property
            def ok(self):
                return True

        return DummyResp()

    monkeypatch.delenv("MERCADOPAGO_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr("routes.mercadopago_webhook.requests.get", fake_get)

    resp = client.post(
        "/mercadopago_webhook",
        data=json.dumps(
            {
                "type": "payment",
                "tenant_id": tenant.id,
                "data": {"id": "pay-2"},
            }
        ),
        content_type="application/json",
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["estado"] == "pagado"
    assert captured["auth_calls"][0] == "Bearer tenant-token"


@pytest.mark.usefixtures("client")
def test_webhook_rejects_amount_mismatch_without_updating_order(client, tenant_with_pedido, monkeypatch):
    tenant, pedido = tenant_with_pedido

    def fake_get(_url, headers=None, **_kwargs):
        class DummyResp:
            status_code = 200
            ok = True

            def json(self):
                return {
                    "external_reference": str(pedido.id),
                    "status": "approved",
                    "transaction_amount": 999,
                    "currency_id": "ARS",
                    "metadata": {"tenant_id": tenant.id},
                }

        return DummyResp()

    monkeypatch.setenv("MERCADOPAGO_ACCESS_TOKEN", "fallback-token")
    monkeypatch.setattr("routes.mercadopago_webhook.requests.get", fake_get)

    resp = client.post(
        "/mercadopago_webhook",
        json={"type": "payment", "data": {"id": "pay-wrong-amount"}},
    )

    assert resp.status_code == 409
    assert resp.get_json()["reason_code"] == "payment_amount_mismatch"
    db.session.refresh(pedido)
    assert pedido.estado == "pendiente_pago"
    assert pedido.mp_payment_id is None
