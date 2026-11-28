"""WhatsApp catalog Flow: rubro, product, quantity and a cart that becomes an order."""
from __future__ import annotations

import pytest

from app import db
from models import CatalogoItem, MarketOrder, OrderEvent, WhatsAppFlowInteraction
from services.meta_flow_data_exchange import MetaFlowActionError
from services.meta_flow_json import build_catalog_order_flow
from services.meta_flow_runtime import (
    CATALOG_FLOW_ID,
    SUPPORTED_FLOW_IDS,
    _catalog_category_response,
    _catalog_product_response,
    _catalog_selection,
    apply_whatsapp_flow_completion,
)
from tests.test_meta_flow_completion import _interaction, _submission, _tenant_scope


def _store(slug="mercado-flow"):
    tenant, sender = _tenant_scope(slug)
    rows = [
        ("Yerba mate 1 kg", "Almacén", "5200", "48", True),
        ("Arroz largo fino 1 kg", "Almacén", "1850", "80", True),
        ("Agua mineral 2 L", "Bebidas", "$ 1.100", "120", True),
        ("Juguete mordillo", "Mascotas", "4500", "0", True),      # out of stock
        ("Sin precio", "Mascotas", "", "5", True),               # no price
        ("Oculto", "Bebidas", "999", "5", False),                # not published
    ]
    items = []
    for name, category, price, stock, available in rows:
        item = CatalogoItem(user_id=tenant.municipio_id, tenant_id=tenant.id, sku=name[:8], nombre=name,
                            categoria=category, precio=price, cantidad=stock, disponible=available,
                            precio_monetario=0.0, modalidad="venta")
        db.session.add(item)
        items.append(item)
    db.session.commit()
    return tenant, sender, {item.nombre: item for item in items}


def test_flow_is_supported_and_compiles_without_cycles():
    assert CATALOG_FLOW_ID in SUPPORTED_FLOW_IDS
    artifact = build_catalog_order_flow()
    assert [screen["id"] for screen in artifact.document["screens"]] == [
        "CATALOG_CATEGORY", "CATALOG_PRODUCT", "CATALOG_CART"]


def test_only_sellable_products_are_offered(client):
    tenant, _, items = _store()
    categories = _catalog_category_response(tenant)["data"]["categories"]
    assert [(c["id"], c["title"], c["description"]) for c in categories] == [
        ("almacen", "Almacén", "2 productos"), ("bebidas", "Bebidas", "1 producto")]
    products = _catalog_product_response(tenant, "almacen")["data"]["products"]
    assert [p["title"] for p in products] == ["Arroz largo fino 1 kg", "Yerba mate 1 kg"]
    assert products[0]["description"].startswith("$ 1.850")
    with pytest.raises(MetaFlowActionError):
        _catalog_product_response(tenant, "mascotas")
    with pytest.raises(MetaFlowActionError):
        _catalog_selection(tenant, {"product": str(items["Oculto"].id), "quantity": "1"})
    with pytest.raises(MetaFlowActionError):
        _catalog_selection(tenant, {"product": str(items["Yerba mate 1 kg"].id), "quantity": "11"})


def test_cart_grows_across_forms_and_finishes_as_an_order_with_catalog_prices(client):
    tenant, sender, items = _store("mercado-flow-2")
    first = _interaction(tenant=tenant, sender=sender, flow_id=CATALOG_FLOW_ID,
                         metadata={"catalog_selection": {"product_id": items["Yerba mate 1 kg"].id, "quantity": 2}},
                         data_contract=["next_step"])
    result = apply_whatsapp_flow_completion(tenant_id=tenant.id, interaction_id=first.id,
                                            submission=_submission(first, {"next_step": "agregar"}))
    db.session.commit()
    assert result["fuente"] == "whatsapp_flow_catalog_completed"
    draft = MarketOrder.query.filter_by(tenant_id=tenant.id, status="draft").one()
    assert [(line.name_snapshot, line.quantity, float(line.price_monetary)) for line in draft.items] == [("Yerba mate 1 kg", 2, 5200.0)]

    second = WhatsAppFlowInteraction(
        tenant_id=tenant.id, template_registry_id=first.template_registry_id, provider_sender_id=sender.id,
        flow_id=CATALOG_FLOW_ID, meta_flow_id=first.meta_flow_id, content_sid=first.content_sid,
        recipient_hash=first.recipient_hash, recipient_hint="***1234", token_digest="f" * 64,
        idempotency_key="second", status="sent", data_contract=["next_step"],
        metadata_json={"catalog_selection": {"product_id": items["Agua mineral 2 L"].id, "quantity": 3}},
        expires_at=first.expires_at)
    db.session.add(second)
    db.session.commit()
    result = apply_whatsapp_flow_completion(tenant_id=tenant.id, interaction_id=second.id,
                                            submission=_submission(second, {"next_step": "finalizar"}))
    db.session.commit()
    order = db.session.get(MarketOrder, draft.id)
    assert order.status == "pending" and float(order.total_monetary) == 2 * 5200 + 3 * 1100
    assert len(order.items) == 2 and order.metadata_payload["ready_for_checkout"] is True
    assert OrderEvent.query.filter_by(market_order_id=order.id, type="whatsapp_catalog_cart_finished").count() == 1
    assert "Total: $ 13.700" in result["message_body"]


def test_a_submission_cannot_set_prices_or_skip_the_choice(client):
    tenant, sender, items = _store("mercado-flow-3")
    empty = _interaction(tenant=tenant, sender=sender, flow_id=CATALOG_FLOW_ID, metadata={}, data_contract=["next_step"])
    with pytest.raises(MetaFlowActionError):
        apply_whatsapp_flow_completion(tenant_id=tenant.id, interaction_id=empty.id,
                                       submission=_submission(empty, {"next_step": "finalizar"}))


@pytest.mark.parametrize("raw, expected", [("1850", "1850.00"), ("$ 1.100", "1100.00"), ("2.900,50", "2900.50"),
                                           ("12.5", "12.50"), ("1.250.000", "1250000.00"), ("", None), ("0", None)])
def test_catalog_prices_read_argentine_notation(raw, expected):
    from types import SimpleNamespace
    from services.meta_flow_runtime import _catalog_price
    value = _catalog_price(SimpleNamespace(precio=raw))
    assert (str(value) if value is not None else None) == expected
