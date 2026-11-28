"""Store on the Conversa test number: buy, redeem points or donate from WhatsApp.

Off unless both ``META_TDF_SANDBOX_MARKET_FLOW_ID`` (the published «Tienda» Flow)
and ``META_TDF_SANDBOX_MARKET_TENANT_SLUG`` (whose published catalog it shows)
are set. The contact is the pilot's pseudonym; no number is stored here.

Commands: TIENDA (or DONAR, CANJEAR, COMPRAR) opens the form; the form adds to
the cart; LISTO sends it; PEDIDO shows the cart or how the last order is going.
"""
from __future__ import annotations

import json
import unicodedata

from flask import current_app

from models import TenantProfile
from services import market_vouchers as vouchers
from services import marketplace_whatsapp_flow as market

FLOW_ID_KEY = "META_TDF_SANDBOX_MARKET_FLOW_ID"
TENANT_KEY = "META_TDF_SANDBOX_MARKET_TENANT_SLUG"
DRAFT_KEY = "META_TDF_SANDBOX_MARKET_FLOW_DRAFT"
FLOW_MARKER = {"tdf_market": market.SCREEN_ID}
FLOW_CTA = "Abrir la tienda"
VOUCHER_URL = "https://www.chatboc.ar/v/"
MENU_HINT = "🛍️ Escribí TIENDA para comprar, canjear tus puntos o donar."

_OPEN = {"tienda", "catalogo", "donar", "donacion", "donaciones", "canjear", "canje", "puntos", "comprar"}
_FINISH = {"listo", "terminar", "finalizar", "enviar pedido", "confirmar pedido"}
_STATUS = {"pedido", "mi pedido", "carrito", "mi carrito", "estado del pedido"}


def _plain(text):
    folded = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(folded.casefold().strip(" .!¡?¿").split())


def flow_id():
    value = current_app.config.get(FLOW_ID_KEY)
    return value if isinstance(value, str) and value.isdigit() else ""


def store_tenant():
    slug = current_app.config.get(TENANT_KEY)
    if not flow_id() or not isinstance(slug, str) or not slug.strip():
        return None
    tenant = TenantProfile.query.filter_by(slug=slug.strip()).first()
    return tenant if tenant is not None and tenant.is_active is True else None


def enabled():
    return store_tenant() is not None


def _store_name(tenant):
    return str(getattr(tenant, "nombre", "") or "la tienda")[:60]


def _open(tenant, lead=""):
    body = (lead + "\n\n" if lead else "") + f"🛍️ Tienda de {_store_name(tenant)}. Tocá el botón para elegir."
    return body[:1024], (FLOW_MARKER,)


def _with_cart(order):
    return market.cart_text(order)


def answer(event, context, contact_key, clear):
    """(body, context, revision, buttons) for a store message, or None if it is not one."""
    tenant = store_tenant()
    if tenant is None:
        return None
    if event.content_type == "interactive":
        selection = event.selection if isinstance(event.selection, str) else ""
        if not selection.startswith("flow:"):
            return None
        try:
            chosen = json.loads(selection[len("flow:"):])
        except ValueError:
            return None
        if not isinstance(chosen, dict) or "producto" not in chosen:
            return None
        choice = market.parse_choice(chosen)
        if choice is None:
            body, buttons = _open(tenant, "No pude leer esa elección. Probá de nuevo.")
            return body, clear(context), None, buttons
        try:
            order = market.add_choice(tenant_id=tenant.id, pseudonym=contact_key,
                                      product_id=choice[0], quantity=choice[1])
        except market.MarketplaceChoiceError as error:
            lead = ("No hay stock suficiente para esa cantidad." if str(error) == "marketplace_stock_insufficient"
                    else "Esa opción ya no está disponible.")
            body, buttons = _open(tenant, lead)
            return body, clear(context), None, buttons
        lead = "✅ Agregado. Tu pedido:\n" + _with_cart(order) + "\n\nEscribí LISTO para enviarlo, o sumá algo más."
        body, buttons = _open(tenant, lead)
        return body, clear(context), None, buttons
    if event.content_type != "text":
        return None
    command = _plain(event.text)
    if command in _OPEN:
        body, buttons = _open(tenant)
        return body, clear(context), None, buttons
    if command in _FINISH:
        order = market.finish(tenant_id=tenant.id, pseudonym=contact_key)
        if order is None:
            body, buttons = _open(tenant, "Todavía no sumaste nada a tu pedido.")
            return body, clear(context), None, buttons
        has_points = any(line.price_points for line in order.items)
        issued = vouchers.issue_for_order(order)
        codes = ""
        if issued:
            codes = "\n\n🎟️ Tus códigos (mostralos al retirar o al llegar):\n" + "\n".join(
                f"• {item['name']}: {item['display']}\n  {VOUCHER_URL}{item['code']}" for item in issued)
        body = (f"📦 Enviamos tu pedido #{order.id} a {_store_name(tenant)}:\n" + _with_cart(order) + codes
                + ("\n\nLos puntos se validan con tu cuenta de participación del portal ciudadano." if has_points else "")
                + "\n\nEscribí PEDIDO para ver cómo sigue.")
        return body[:4096], clear(context), None, ()
    if command in _STATUS:
        order = market.latest_order(tenant.id, contact_key)
        if order is None:
            body, buttons = _open(tenant, "Todavía no tenés pedidos.")
            return body, clear(context), None, buttons
        if order.status == "draft":
            body, buttons = _open(tenant, "Tu pedido (sin enviar):\n" + _with_cart(order) + "\n\nEscribí LISTO para enviarlo.")
            return body, clear(context), None, buttons
        body = f"📦 Pedido #{order.id}: {market.status_label(order)}.\n" + _with_cart(order)
        return body[:4096], clear(context), None, ()
    return None
