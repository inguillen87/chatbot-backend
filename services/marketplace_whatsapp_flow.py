"""WhatsApp Flow «Tienda» for any published catalog: pymes, municipalities and agencies.

A municipality's marketplace works like a hardware store's: people buy, redeem
points (a school kit, a scholarship) or sign up to donate. This module turns a
tenant's published catalog into one static WhatsApp form and keeps the cart:

* one screen, no data endpoint: product and quantity, completed with short ids;
* each option says what it costs in its own terms: pesos, points or «Donación»;
* the completion payload is ``{"producto": "p<id>", "cantidad": "<n>"}``;
* the cart is a draft MarketOrder bound to a pseudonymous contact key, so a new
  form adds to the same cart. Prices and points always come from the catalog,
  never from the submission.

Nothing here talks to Meta.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from services.meta_flow_json import FlowBlueprint, FlowJsonArtifact, compile_flow_blueprint

MAX_OPTIONS = 20
MAX_CATEGORIES_TOTAL = 10
MAX_QUANTITY = 10
TITLE_MAX = 30
SCREEN_ID = "TIENDA"
CONTRACT = "chatboc.marketplace_whatsapp_flow.v1"
_PRODUCT_ID = re.compile(r"p([1-9][0-9]{0,9})")

MONEY_MODES = {"venta", "compra", "pago", "precio"}
POINTS_MODES = {"canje", "puntos"}
DONATION_MODES = {"donacion", "donación", "donar"}
FREE_MODES = {"inscripcion", "inscripción", "gratis", "turno"}
FREE_PRICE_WORDS = {"gratis", "sin cargo", "gratuito", "gratuita", "libre"}


def _plain(value: Any) -> str:
    return unicodedata.normalize("NFD", str(value or "")).encode("ascii", "ignore").decode().lower().strip()


def _title(value: Any, limit: int = TITLE_MAX) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_price(raw: Any) -> Decimal | None:
    """Argentine notation first: dots group thousands, a comma marks cents."""
    if isinstance(raw, (int, float, Decimal)):
        try:
            value = Decimal(str(raw)).quantize(Decimal("0.01"))
        except InvalidOperation:
            return None
        return value if value > 0 else None
    digits = re.sub(r"[^0-9,.]", "", str(raw or "").strip())
    if not digits:
        return None
    if "," in digits:
        digits = digits.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", digits):
        digits = digits.replace(".", "")
    try:
        value = Decimal(digits).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def format_money(value: Decimal) -> str:
    whole, fraction = f"{value.quantize(Decimal('0.01')):.2f}".split(".")
    groups = []
    while whole:
        groups.append(whole[-3:])
        whole = whole[:-3]
    text = ".".join(reversed(groups))
    return f"$ {text}" if fraction == "00" else f"$ {text},{fraction}"


def format_points(value: int) -> str:
    return f"{value:,}".replace(",", ".") + (" punto" if value == 1 else " puntos")


@dataclass(frozen=True)
class Offer:
    id: int
    name: str
    category: str
    mode: str  # "money" | "points" | "donation" | "free" | "quote"
    price: Decimal | None
    points: int | None
    stock: int | None
    # How many one person may take. Benefits (points, free sign-ups) default to
    # one, so a family cannot empty the school-kit stock; sales are unlimited.
    max_per_person: int | None = None

    @property
    def option_id(self) -> str:
        return f"p{self.id}"

    def unit_label(self) -> str:
        if self.mode == "points" and self.points:
            return format_points(self.points)
        if self.mode == "money" and self.price is not None:
            return format_money(self.price)
        if self.mode == "donation":
            return f"Donás {format_points(self.points)}" if self.points else "Donación"
        if self.mode == "free":
            return "Gratis · inscripción"
        return "Precio a confirmar"

    def option_label(self) -> str:
        label = self.unit_label()
        if self.max_per_person:
            label += f" · máx. {self.max_per_person} por persona"
        return label


def _mode(item: Any) -> tuple[str, Decimal | None, int | None]:
    modality = _plain(getattr(item, "modalidad", "") or "venta")
    points = getattr(item, "precio_puntos", None)
    points = int(points) if isinstance(points, int) and points > 0 else None
    price = parse_price(getattr(item, "precio", None))
    if price is None:
        try:
            price = parse_price(getattr(item, "precio_monetario", None))
        except Exception:  # deferred column missing in an old schema
            price = None
    if modality in DONATION_MODES:
        return "donation", None, points
    if modality in FREE_MODES or (price is None and _plain(getattr(item, "precio", "")) in FREE_PRICE_WORDS):
        return "free", None, None
    if modality in POINTS_MODES:
        return ("points", None, points) if points else ("quote", None, None)
    if price is not None:
        return "money", price, None
    if points:
        return "points", None, points
    return "quote", None, None


def _stock(item: Any) -> int | None:
    raw = str(getattr(item, "cantidad", "") or "").strip()
    return int(raw) if raw.isdigit() else None


LIMITED_BY_DEFAULT = {"points", "free"}


def _max_per_person(item: Any, mode: str) -> int | None:
    metadata = getattr(item, "extra_metadata", None)
    raw = metadata.get("max_per_person") if isinstance(metadata, dict) else None
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, str) and raw.strip().isdigit():
        raw = int(raw.strip())
    if isinstance(raw, int):
        return None if raw <= 0 else min(raw, MAX_QUANTITY)
    modality = _plain(getattr(item, "modalidad", "") or "")
    return 1 if mode in LIMITED_BY_DEFAULT or modality in POINTS_MODES | FREE_MODES else None


def offers_from_items(items: Sequence[Any]) -> list[Offer]:
    """Published, available items, at most twenty, grouped by category."""
    offers: list[Offer] = []
    for item in items:
        if getattr(item, "disponible", True) is False or _stock(item) == 0:
            continue
        name = " ".join(str(getattr(item, "nombre", "") or "").split())
        if not name:
            continue
        mode, price, points = _mode(item)
        offers.append(Offer(id=int(item.id), name=name, category=_title(getattr(item, "categoria", "") or "Otros", 40),
                            mode=mode, price=price, points=points, stock=_stock(item),
                            max_per_person=_max_per_person(item, mode)))
    offers.sort(key=lambda offer: (_plain(offer.category), _plain(offer.name)))
    return offers[:MAX_OPTIONS * MAX_CATEGORIES_TOTAL]


def tenant_offers(tenant_id: int) -> list[Offer]:
    from models import CatalogoItem

    items = (CatalogoItem.query.filter_by(tenant_id=int(tenant_id), disponible=True)
             .order_by(CatalogoItem.categoria, CatalogoItem.nombre).limit(200).all())
    return offers_from_items(items)


def _intro(offers: Sequence[Offer]) -> str:
    modes = {offer.mode for offer in offers}
    verbs = []
    if modes & {"money", "quote"}:
        verbs.append("comprar")
    if "points" in modes:
        verbs.append("canjear tus puntos")
    if "free" in modes:
        verbs.append("anotarte")
    if "donation" in modes:
        verbs.append("donar")
    if not verbs:
        verbs = ["pedir"]
    joined = verbs[0] if len(verbs) == 1 else ", ".join(verbs[:-1]) + " o " + verbs[-1]
    return f"Elegí qué querés {joined}. Lo sumamos a tu pedido en este chat."


MAX_CATEGORIES = 10
NAV_TITLE_MAX = 30
NAV_DESCRIPTION_MAX = 20
NAV_METADATA_MAX = 80


def _category_hint(offers: Sequence[Offer]) -> str:
    """«Desde 400 puntos», «Gratis», «Desde $ 2.000», or the mix, for one category."""
    points = [offer.points for offer in offers if offer.mode == "points" and offer.points]
    prices = [offer.price for offer in offers if offer.mode == "money" and offer.price is not None]
    parts = []
    if points:
        parts.append(f"Desde {format_points(min(points))}")
    if prices:
        parts.append(f"Desde {format_money(min(prices))}")
    if any(offer.mode == "free" for offer in offers):
        parts.append("Gratis")
    if any(offer.mode == "donation" for offer in offers):
        parts.append("Donaciones")
    return _title(" · ".join(parts), NAV_METADATA_MAX)


def _screen_quantity_limit(offers: Sequence[Offer]) -> int:
    """The largest quantity anyone may pick on a screen (unlimited items allow the maximum)."""
    limits = [offer.max_per_person or MAX_QUANTITY for offer in offers[:MAX_OPTIONS]]
    return max(limits) if limits else MAX_QUANTITY


def _product_children(title: str, offers: Sequence[Offer], intro: str, *,
                      with_category: bool = False) -> list[dict[str, Any]]:
    shown = list(offers[:MAX_OPTIONS])
    options = [{"id": offer.option_id, "title": _title(offer.name),
                "description": _title(f"{offer.option_label()} · {offer.category}" if with_category
                                      else offer.option_label(), 72)} for offer in shown]
    top = _screen_quantity_limit(shown)
    quantity_children: list[dict[str, Any]] = []
    payload = {"producto": "${form.producto}"}
    if top > 1:
        payload["cantidad"] = "${form.cantidad}"
        quantities = [{"id": str(n), "title": str(n)} for n in range(1, top + 1)]
        quantity_children = [{"type": "Dropdown", "name": "cantidad", "label": "Cantidad", "required": True,
                              "data-source": quantities}]
    # One each: no quantity to ask, and the completion carries only the choice.
    return [
        {"type": "TextHeading", "text": _title(title, 80)},
        {"type": "TextBody", "text": intro},
        {"type": "RadioButtonsGroup", "name": "producto", "label": "¿Qué elegís?", "required": True,
         "data-source": options},
        *quantity_children,
        {"type": "TextCaption", "text": "Podés sumar más cosas después. No hace falta compartir datos personales."},
        {"type": "Footer", "label": "Agregar al pedido",
         "on-click-action": {"name": "complete",
                             "payload": payload}},
    ]


def build_marketplace_blueprint(*, store_name: str, offers: Sequence[Offer]) -> FlowBlueprint:
    """Several categories: a first screen to browse them, one screen per category.

    Same completion payload either way ({producto, cantidad}), so the chat side
    does not change. A single category keeps the one-screen form."""
    if not offers:
        raise ValueError("catalog_without_offers")
    groups: dict[str, list[Offer]] = {}
    for offer in offers:
        groups.setdefault(offer.category, []).append(offer)
    if 1 < len(groups) <= MAX_CATEGORIES:
        slug = re.sub(r"[^a-z0-9]+", "_", _plain(store_name)).strip("_")[:40] or "tienda"
        items, screens = [], []
        for index, (category, members) in enumerate(groups.items(), start=1):
            # Meta screen ids take letters and underscores only.
            screen_id = f"CATEGORIA_{chr(ord('A') + index - 1)}"
            count = len(members)
            items.append({
                "id": f"c{index}",
                "main-content": {
                    "title": _title(category, NAV_TITLE_MAX),
                    "description": _title(f"{count} opci{'ón' if count == 1 else 'ones'}", NAV_DESCRIPTION_MAX),
                    "metadata": _category_hint(members),
                },
                "on-click-action": {"name": "navigate", "next": {"type": "screen", "name": screen_id}, "payload": {}},
            })
            screens.append({
                "id": screen_id,
                "title": _title(category) or "Opciones",
                "terminal": True,
                "success": True,
                "data": {},
                "layout": {"type": "SingleColumnLayout",
                           "children": _product_children(
                               category, members,
                               "Elegí una opción y la cantidad." if _screen_quantity_limit(members) > 1
                               else "Elegí una opción.")},
            })
        home = {
            "id": SCREEN_ID,
            "title": _title(store_name) or "Tienda",
            "data": {},
            # Meta: a screen with a NavigationList holds nothing else, so the
            # heading and the intro travel as the list's own label and description.
            "layout": {"type": "SingleColumnLayout", "children": [
                {"type": "NavigationList", "name": "categorias", "label": _title(store_name, 80) or "Tienda",
                 "description": _intro(offers)[:300], "list-items": items},
            ]},
        }
        return FlowBlueprint(name=f"tienda_{slug}", endpoint_driven=False, screens=(home, *screens))
    return _single_screen_blueprint(store_name=store_name, offers=offers)


def _single_screen_blueprint(*, store_name: str, offers: Sequence[Offer]) -> FlowBlueprint:
    children = _product_children(store_name or "Tienda", offers, _intro(offers), with_category=True)
    slug = re.sub(r"[^a-z0-9]+", "_", _plain(store_name)).strip("_")[:40] or "tienda"
    return FlowBlueprint(
        name=f"tienda_{slug}",
        endpoint_driven=False,
        screens=({
            "id": SCREEN_ID,
            "title": _title(store_name) or "Tienda",
            "terminal": True,
            "success": True,
            "data": {},
            "layout": {"type": "SingleColumnLayout", "children": children},
        },),
    )


def build_marketplace_flow(*, store_name: str, offers: Sequence[Offer]) -> FlowJsonArtifact:
    return compile_flow_blueprint(build_marketplace_blueprint(store_name=store_name, offers=offers))


def flow_summary(offers: Sequence[Offer]) -> dict[str, Any]:
    return {
        "offers": [{"id": offer.option_id, "name": offer.name, "category": offer.category,
                    "mode": offer.mode, "label": offer.unit_label()} for offer in offers],
        "completion_payload": ["producto", "cantidad"],
    }


def parse_choice(chosen: Mapping[str, Any]) -> tuple[int, int] | None:
    """``{"producto": "p168", "cantidad": "2"}`` → (168, 2); without a quantity it is one."""
    product, quantity = chosen.get("producto"), chosen.get("cantidad", "1")
    if not isinstance(product, str) or not isinstance(quantity, str):
        return None
    match = _PRODUCT_ID.fullmatch(product)
    if match is None or not quantity.isdigit() or not 1 <= int(quantity) <= MAX_QUANTITY:
        return None
    return int(match.group(1)), int(quantity)


# --- cart ---------------------------------------------------------------------------------

class MarketplaceChoiceError(ValueError):
    pass


def _contact_key(pseudonym: str) -> str:
    return f"wa_flow:{pseudonym}"[:160]


def draft_order(tenant_id: int, pseudonym: str) -> Any:
    from models import MarketOrder

    return (MarketOrder.query.filter_by(tenant_id=int(tenant_id), status="draft", channel="whatsapp",
                                        contact_key=_contact_key(pseudonym))
            .order_by(MarketOrder.id.desc()).first())


def latest_order(tenant_id: int, pseudonym: str) -> Any:
    from models import MarketOrder

    return (MarketOrder.query.filter_by(tenant_id=int(tenant_id), channel="whatsapp",
                                        contact_key=_contact_key(pseudonym))
            .order_by(MarketOrder.id.desc()).first())


def cart_text(order: Any) -> str:
    lines, money, points = [], Decimal("0"), 0
    for line in (order.items if order is not None else []):
        quantity = int(line.quantity or 1)
        name = _title(line.name_snapshot or "Producto", 40)
        if line.price_points:
            subtotal = int(line.price_points) * quantity
            points += subtotal
            verb = "Donás " if (line.modalidad or "") == "donacion" else ""
            lines.append(f"• {quantity} x {name} · {verb}{format_points(subtotal)}")
        elif line.price_monetary:
            subtotal = Decimal(str(line.price_monetary)) * quantity
            money += subtotal
            lines.append(f"• {quantity} x {name} · {format_money(subtotal)}")
        elif (line.modalidad or "") == "donacion":
            lines.append(f"• {quantity} x {name} · Donación")
        elif (line.modalidad or "") == "inscripcion":
            lines.append(f"• {quantity} x {name} · Inscripción gratis")
        else:
            lines.append(f"• {quantity} x {name} · Precio a confirmar")
    if not lines:
        return "Tu pedido está vacío."
    totals = []
    if money:
        totals.append(format_money(money))
    if points:
        totals.append(format_points(points))
    text = "\n".join(lines)
    return text + ("\nTotal: " + " + ".join(totals) if totals else "")


_MODALIDAD = {"money": "venta", "quote": "venta", "points": "canje", "donation": "donacion", "free": "inscripcion"}


LIMIT_IGNORED_STATUSES = ("draft", "cancelled", "rejected", "expired")


def _already_taken(tenant_id: int, pseudonym: str, product_id: int, *, exclude: int | None = None) -> int:
    """Units of this item the same contact already ordered (sent orders that were not cancelled)."""
    from sqlalchemy import func
    from models import db, MarketOrder, MarketOrderItem

    query = (db.session.query(func.coalesce(func.sum(MarketOrderItem.quantity), 0))
             .join(MarketOrder, MarketOrder.id == MarketOrderItem.order_id)
             .filter(MarketOrder.tenant_id == int(tenant_id), MarketOrder.contact_key == _contact_key(pseudonym),
                     MarketOrderItem.product_id == int(product_id),
                     ~MarketOrder.status.in_(LIMIT_IGNORED_STATUSES)))
    if exclude is not None:
        query = query.filter(MarketOrder.id != int(exclude))
    return int(query.scalar() or 0)


def add_choice(*, tenant_id: int, pseudonym: str, product_id: int, quantity: int) -> Any:
    """Add a catalog choice to this contact's WhatsApp cart (a draft MarketOrder)."""
    from models import db, MarketOrder, MarketOrderItem

    offer = next((entry for entry in tenant_offers(tenant_id) if entry.id == int(product_id)), None)
    if offer is None or not 1 <= int(quantity) <= MAX_QUANTITY:
        raise MarketplaceChoiceError("marketplace_choice_invalid")
    order = draft_order(tenant_id, pseudonym)
    if order is None:
        order = MarketOrder(tenant_id=int(tenant_id), status="draft", channel="whatsapp", currency="ARS",
                            contact_key=_contact_key(pseudonym),
                            metadata_payload={"source": "whatsapp_marketplace_flow"})
        db.session.add(order)
        db.session.flush()
    existing = next((line for line in order.items if int(line.product_id or 0) == offer.id), None)
    in_cart = int(existing.quantity or 0) if existing is not None else 0
    if offer.max_per_person:
        allowed = offer.max_per_person - in_cart - _already_taken(tenant_id, pseudonym, offer.id, exclude=order.id)
        if allowed <= 0:
            raise MarketplaceChoiceError("marketplace_limit_reached")
        quantity = min(int(quantity), allowed)
    total_quantity = int(quantity) + in_cart
    if offer.stock is not None and total_quantity > offer.stock:
        raise MarketplaceChoiceError("marketplace_stock_insufficient")
    if existing is not None:
        existing.quantity = min(MAX_QUANTITY * 5, total_quantity)
    else:
        db.session.add(MarketOrderItem(order_id=order.id, product_id=offer.id, quantity=int(quantity),
                                       price_monetary=offer.price, price_points=offer.points, currency="ARS",
                                       modalidad=_MODALIDAD[offer.mode], name_snapshot=offer.name[:255]))
    db.session.flush()
    db.session.refresh(order)
    _refresh_totals(order)
    return order


def _refresh_totals(order: Any) -> None:
    money = sum((Decimal(str(line.price_monetary)) * int(line.quantity or 1)
                 for line in order.items if line.price_monetary), Decimal("0"))
    points = sum(int(line.price_points) * int(line.quantity or 1) for line in order.items if line.price_points)
    order.total_monetary = money
    if hasattr(order, "total_points"):
        order.total_points = points


def finish(*, tenant_id: int, pseudonym: str) -> Any:
    """Close the cart: the organization receives it as a pending order."""
    from models import db, OrderEvent

    order = draft_order(tenant_id, pseudonym)
    if order is None or not order.items:
        return None
    order.status = "pending"
    metadata = dict(order.metadata_payload or {})
    metadata["ready_for_checkout"] = True
    metadata["payment"] = "to_agree"
    order.metadata_payload = metadata
    db.session.add(OrderEvent(market_order_id=order.id, type="whatsapp_marketplace_order_finished",
                              payload={"items": len(order.items)}))
    db.session.add(order)
    return order


STATUS_LABELS = {
    "draft": "armándose (todavía no lo enviaste)",
    "pending": "recibido, lo están revisando",
    "pending_payment": "recibido, falta coordinar el pago",
    "confirmed": "confirmado",
    "paid": "pagado",
    "ready": "listo para retirar o entregar",
    "delivered": "entregado",
    "cancelled": "cancelado",
}


def status_label(order: Any) -> str:
    return STATUS_LABELS.get(str(getattr(order, "status", "") or ""), str(getattr(order, "status", "") or "en curso"))
