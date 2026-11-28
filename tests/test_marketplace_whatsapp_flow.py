"""«Tienda» WhatsApp Flow: one catalog form for pymes and municipalities (buy, redeem, donate)."""
from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from services import marketplace_whatsapp_flow as market


def item(id_, nombre, categoria, *, precio=None, modalidad="venta", puntos=None, cantidad=None, disponible=True):
    return SimpleNamespace(id=id_, nombre=nombre, categoria=categoria, precio=precio, modalidad=modalidad,
                           precio_puntos=puntos, cantidad=cantidad, disponible=disponible, precio_monetario=None)


MUNICIPAL = [
    item(1, "Bolsón saludable kilómetro cero", "Producción local", precio="3.500", modalidad="compra"),
    item(2, "Bono de donación Hospital Saporiti", "Salud", modalidad="canje", puntos=2500),
    item(3, "Kit escolar solidario", "Educación", modalidad="canje", puntos=1500),
    item(4, "Árbol nativo en tu vereda", "Ambiente", modalidad="donacion"),
    item(5, "Beca de estudio", "Educación", modalidad="canje", puntos=None),
    item(6, "Sin stock", "Otros", precio="100", cantidad="0"),
    item(7, "Oculto", "Otros", precio="100", disponible=False),
]


def test_each_offer_speaks_its_own_terms():
    offers = {offer.id: offer for offer in market.offers_from_items(MUNICIPAL)}
    assert set(offers) == {1, 2, 3, 4, 5}
    assert offers[1].unit_label() == "$ 3.500"
    assert offers[2].unit_label() == "2.500 puntos"
    assert offers[4].unit_label() == "Donación"
    assert offers[5].unit_label() == "Precio a confirmar"


def test_form_is_static_and_says_what_people_can_do():
    offers = market.offers_from_items(MUNICIPAL)
    flow = json.loads(market.build_marketplace_flow(store_name="Municipalidad de Junín", offers=offers).as_bytes())
    screen = flow["screens"][0]
    assert screen["id"] == "TIENDA" and screen["terminal"] is True and "data_api_version" not in flow
    texts = [child.get("text") for child in screen["layout"]["children"]]
    assert "Elegí qué querés comprar, canjear tus puntos o donar. Lo sumamos a tu pedido en este chat." in texts
    radio = next(child for child in screen["layout"]["children"] if child["type"] == "RadioButtonsGroup")
    assert radio["data-source"][0]["id"].startswith("p") and all(len(o["title"]) <= 30 for o in radio["data-source"])
    footer = screen["layout"]["children"][-1]
    assert footer["on-click-action"]["payload"] == {"producto": "${form.producto}", "cantidad": "${form.cantidad}"}


def test_form_refuses_an_empty_catalog_and_caps_options():
    with pytest.raises(ValueError):
        market.build_marketplace_flow(store_name="Vacía", offers=[])
    many = [item(i, f"Producto {i}", "Almacén", precio="10") for i in range(1, 40)]
    assert len(market.offers_from_items(many)) == market.MAX_OPTIONS


@pytest.mark.parametrize("chosen,expected", [
    ({"producto": "p12", "cantidad": "3"}, (12, 3)),
    ({"producto": "12", "cantidad": "3"}, None),
    ({"producto": "p12", "cantidad": "0"}, None),
    ({"producto": "p12", "cantidad": "11"}, None),
    ({"producto": "p0", "cantidad": "1"}, None),
    ({"tema": "x"}, None),
])
def test_choice_is_only_ids(chosen, expected):
    assert market.parse_choice(chosen) == expected


def test_prices_follow_argentine_notation():
    assert market.parse_price("$ 2.900") == Decimal("2900.00")
    assert market.parse_price("1.850,50") == Decimal("1850.50")
    assert market.parse_price("gratis") is None
    assert market.format_money(Decimal("1850.50")) == "$ 1.850,50"
