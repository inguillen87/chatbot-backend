"""«Tienda» WhatsApp Flow: one catalog form for pymes and municipalities (buy, redeem, donate)."""
from __future__ import annotations

import json
import re
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


def test_several_categories_browse_first_then_one_screen_each():
    offers = market.offers_from_items(MUNICIPAL)
    flow = json.loads(market.build_marketplace_flow(store_name="Municipalidad de Junín", offers=offers).as_bytes())
    assert "data_api_version" not in flow and "routing_model" not in flow
    home, *categories = flow["screens"]
    assert home["id"] == "TIENDA" and not home.get("terminal")
    assert [child["type"] for child in home["layout"]["children"]] == ["NavigationList"]
    nav = home["layout"]["children"][0]
    assert nav["label"] == "Municipalidad de Junín"
    assert nav["description"] == "Elegí qué querés comprar, canjear tus puntos o donar. Lo sumamos a tu pedido en este chat."
    by_title = {item["main-content"]["title"]: item for item in nav["list-items"]}
    assert by_title["Educación"]["main-content"]["description"] == "2 opciones"
    assert by_title["Educación"]["main-content"]["metadata"] == "Desde 1.500 puntos"
    assert by_title["Producción local"]["main-content"]["metadata"] == "Desde $ 3.500"
    assert {item["on-click-action"]["next"]["name"] for item in nav["list-items"]} == {screen["id"] for screen in categories}
    assert all(re.fullmatch(r"[A-Z_]+", screen["id"]) for screen in flow["screens"])
    by_id = {screen["id"]: screen for screen in categories}
    screen_of = {item["main-content"]["title"]: by_id[item["on-click-action"]["next"]["name"]]
                 for item in nav["list-items"]}
    for screen in categories:
        assert screen["terminal"] is True
    # Selling asks for a quantity; benefits are one per person, so no quantity at all.
    sale = screen_of["Producción local"]["layout"]["children"]
    assert sale[-1]["on-click-action"]["payload"] == {"producto": "${form.producto}", "cantidad": "${form.cantidad}"}
    benefit = screen_of["Educación"]["layout"]["children"]
    assert "Dropdown" not in [child["type"] for child in benefit]
    assert benefit[-1]["on-click-action"]["payload"] == {"producto": "${form.producto}"}
    radio = next(child for child in benefit if child["type"] == "RadioButtonsGroup")
    kit = next(option for option in radio["data-source"] if option["id"] == "p3")
    assert kit["description"] == "1.500 puntos · máx. 1 por persona"


def test_the_organization_can_raise_or_lift_the_limit():
    raised = SimpleNamespace(**vars(item(7, "Entrada recital", "Cultura", modalidad="canje", puntos=600)),
                             extra_metadata={"max_per_person": 4})
    lifted = SimpleNamespace(**vars(item(8, "Charla", "Cultura", modalidad="inscripcion")),
                             extra_metadata={"max_per_person": 0})
    offers = {offer.id: offer for offer in market.offers_from_items([raised, lifted])}
    assert offers[7].max_per_person == 4 and offers[8].max_per_person is None
    children = market._product_children("Cultura", list(offers.values()), "x")
    dropdown = next(child for child in children if child["type"] == "Dropdown")
    assert [option["id"] for option in dropdown["data-source"]] == [str(n) for n in range(1, market.MAX_QUANTITY + 1)]


def test_one_category_keeps_the_single_screen_form():
    offers = market.offers_from_items([item(i, f"Producto {i}", "Almacén", precio="10") for i in range(1, 40)])
    flow = json.loads(market.build_marketplace_flow(store_name="Almacén", offers=offers).as_bytes())
    assert len(flow["screens"]) == 1 and flow["screens"][0]["terminal"] is True
    radio = next(child for child in flow["screens"][0]["layout"]["children"] if child["type"] == "RadioButtonsGroup")
    assert len(radio["data-source"]) == market.MAX_OPTIONS
    with pytest.raises(ValueError):
        market.build_marketplace_flow(store_name="Vacía", offers=[])


@pytest.mark.parametrize("chosen,expected", [
    ({"producto": "p12", "cantidad": "3"}, (12, 3)),
    ({"producto": "p12"}, (12, 1)),
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


def test_validator_refuses_a_navigation_list_with_company():
    from services.meta_flow_json import FlowBlueprint, FlowJsonValidationError, compile_flow_blueprint
    nav = {"type": "NavigationList", "name": "x", "list-items": [{"id": "a", "main-content": {"title": "A"},
           "on-click-action": {"name": "navigate", "next": {"type": "screen", "name": "FIN"}, "payload": {}}}]}
    end = {"id": "FIN", "title": "Fin", "terminal": True, "success": True, "data": {}, "layout": {
        "type": "SingleColumnLayout", "children": [{"type": "Footer", "label": "Listo",
                                                   "on-click-action": {"name": "complete", "payload": {}}}]}}
    home = {"id": "INICIO", "title": "Inicio", "data": {}, "layout": {"type": "SingleColumnLayout",
            "children": [{"type": "TextHeading", "text": "Hola"}, nav]}}
    with pytest.raises(FlowJsonValidationError, match="NAVIGATION_LIST_NOT_ALONE"):
        compile_flow_blueprint(FlowBlueprint(name="x", endpoint_driven=False, screens=(home, end)))
