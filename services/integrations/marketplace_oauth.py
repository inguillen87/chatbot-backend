"""Secure marketplace connections for the Full plan: Mercado Libre, Tiendanube, Shopify.

Replaces the retired transport that put a plain tenant id in OAuth ``state``.

* ``state`` is ``<nonce>.<signature>``: an HMAC over provider, tenant, nonce and
  expiry with a dedicated key (``INTEGRATIONS_OAUTH_KEY_V1``, no fallback).  The
  nonce is recorded server-side and consumed once, so a state cannot be replayed
  or moved to another tenant.
* Provider tokens are sealed with AES-GCM under a key derived from the same
  secret before they reach the database, and are never returned by any API.
* Every provider is configured by its own client id/secret env vars; a provider
  without them reports ``configured: false`` and cannot start a connection.
* Catalog import maps remote products to ``CatalogoItem`` rows (upsert by SKU,
  or ``<PREFIX>-<remote id>`` when the store has no SKU), keeping price, stock,
  photo, category and link.  Prices and stock come from the store each time.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import requests
from flask import current_app

from models import CatalogoItem, IntegrationAccount, IntegrationEvent, TenantProfile, db

STATE_TTL_SECONDS = 10 * 60
IMPORT_MAX_ITEMS = 500
HTTP_TIMEOUT = 15
_KEY_MIN_BYTES = 32
_SHOP_DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}\.myshopify\.com$")
_SHOPIFY_API_VERSION = "2025-01"
_USER_AGENT = "Chatboc (soporte@chatboc.ar)"


class IntegrationError(Exception):
    """A connection or import problem with a stable, user-safe reason code."""

    def __init__(self, reason: str, status: int = 400):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    account_type: str  # IntegrationAccount.type
    sku_prefix: str
    client_id_env: str
    client_secret_env: str
    needs_shop: bool = False
    capabilities: tuple[str, ...] = ("catalogo", "precios", "stock", "fotos")


PROVIDERS: dict[str, Provider] = {
    "mercadolibre": Provider("mercadolibre", "Mercado Libre", "MercadoLibre", "ML",
                             "MERCADOLIBRE_CLIENT_ID", "MERCADOLIBRE_CLIENT_SECRET"),
    "tiendanube": Provider("tiendanube", "Tiendanube", "TiendaNube", "TN",
                           "TIENDANUBE_APP_ID", "TIENDANUBE_CLIENT_SECRET"),
    "shopify": Provider("shopify", "Shopify", "Shopify", "SH",
                        "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", needs_shop=True),
}

# Shown in the status screen so a merchant sees what is coming, never connectable.
UPCOMING = (
    {"key": "woocommerce", "label": "WooCommerce"},
    {"key": "amazon", "label": "Amazon"},
)


def provider(key: str) -> Provider:
    found = PROVIDERS.get(str(key or "").lower())
    if found is None:
        raise IntegrationError("provider_not_supported", 404)
    return found


def _cfg(name: str, default: str = "") -> str:
    value = current_app.config.get(name)
    if value is None:
        value = os.getenv(name, default)
    return str(value or "").strip()


def _secret() -> bytes:
    raw = _cfg("INTEGRATIONS_OAUTH_KEY_V1").encode("utf-8")
    if len(raw) < _KEY_MIN_BYTES:
        raise IntegrationError("integrations_key_missing", 503)
    return raw


def _derive(info: bytes) -> bytes:
    return hmac.new(_secret(), b"chatboc.integrations.v1:" + info, hashlib.sha256).digest()


def provider_configured(item: Provider) -> bool:
    try:
        _secret()
    except IntegrationError:
        return False
    return bool(_cfg(item.client_id_env) and _cfg(item.client_secret_env))


def callback_url(item: Provider) -> str:
    base = _cfg("INTEGRATIONS_CALLBACK_BASE_URL", "https://api.chatboc.ar").rstrip("/")
    return f"{base}/api/integrations/oauth/{item.key}/callback"


def return_url(slug: str, **params: str) -> str:
    base = _cfg("INTEGRATIONS_RETURN_BASE_URL", "https://www.chatboc.ar").rstrip("/")
    query = f"?{urlencode(params)}" if params else ""
    return f"{base}/t/{slug}/integracion{query}"


# --- token sealing -----------------------------------------------------------

def seal(data: Mapping[str, Any]) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    sealed = AESGCM(_derive(b"token-seal")).encrypt(nonce, json.dumps(dict(data)).encode(), b"integration")
    return {"v": "1", "sealed": base64.urlsafe_b64encode(nonce + sealed).decode()}


def unseal(stored: Any) -> dict[str, Any]:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(stored, Mapping) or stored.get("v") != "1":
        raise IntegrationError("integration_reconnect_required", 409)
    try:
        blob = base64.urlsafe_b64decode(str(stored.get("sealed") or ""))
        plain = AESGCM(_derive(b"token-seal")).decrypt(blob[:12], blob[12:], b"integration")
    except (InvalidTag, ValueError):
        raise IntegrationError("integration_reconnect_required", 409) from None
    return json.loads(plain)


# --- signed one-time state -------------------------------------------------

def _state_signature(provider_key: str, tenant_id: int, nonce: str, expires: int) -> str:
    message = f"{provider_key}|{int(tenant_id)}|{nonce}|{int(expires)}".encode()
    return base64.urlsafe_b64encode(hmac.new(_derive(b"oauth-state"), message, hashlib.sha256).digest()).decode().rstrip("=")


def _state_event_provider(item: Provider) -> str:
    return f"oauth_state:{item.key}"


def begin_connection(tenant: TenantProfile, provider_key: str, *, user_id: int | None, shop: str | None = None) -> str:
    """Return the provider's authorization URL after recording a one-time state."""
    item = provider(provider_key)
    if not provider_configured(item):
        raise IntegrationError("provider_not_configured", 409)
    shop_domain = None
    if item.needs_shop:
        shop_domain = normalize_shop(shop)
    nonce = secrets.token_urlsafe(24)
    expires = int(time.time()) + STATE_TTL_SECONDS
    db.session.add(IntegrationEvent(
        tenant_id=int(tenant.id), provider=_state_event_provider(item), event_id=nonce,
        event_type="oauth_state_issued",
        payload={"expires": expires, "user_id": user_id, "shop": shop_domain},
    ))
    db.session.commit()
    state = f"{nonce}.{_state_signature(item.key, int(tenant.id), nonce, expires)}"
    redirect_uri = callback_url(item)
    client_id = _cfg(item.client_id_env)
    if item.key == "mercadolibre":
        return "https://auth.mercadolibre.com.ar/authorization?" + urlencode(
            {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    if item.key == "tiendanube":
        return f"https://www.tiendanube.com/apps/{client_id}/authorize?" + urlencode({"state": state})
    return f"https://{shop_domain}/admin/oauth/authorize?" + urlencode(
        {"client_id": client_id, "scope": "read_products,read_inventory", "redirect_uri": redirect_uri, "state": state})


def normalize_shop(shop: Any) -> str:
    value = str(shop or "").strip().lower()
    value = re.sub(r"^https?://", "", value).split("/")[0]
    if value and "." not in value:
        value = f"{value}.myshopify.com"
    if not _SHOP_DOMAIN.fullmatch(value):
        raise IntegrationError("shopify_shop_invalid", 400)
    return value


def _consume_state(item: Provider, state: str) -> tuple[TenantProfile, Mapping[str, Any]]:
    nonce, _, signature = str(state or "").partition(".")
    if not nonce or not signature or len(nonce) > 64:
        raise IntegrationError("oauth_state_invalid", 400)
    event = IntegrationEvent.query.filter_by(provider=_state_event_provider(item), event_id=nonce).first()
    if event is None:
        raise IntegrationError("oauth_state_invalid", 400)
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    expected = _state_signature(item.key, int(event.tenant_id), nonce, int(payload.get("expires") or 0))
    if not hmac.compare_digest(expected, signature):
        raise IntegrationError("oauth_state_invalid", 400)
    # One use only: the update succeeds for exactly one caller.
    updated = (IntegrationEvent.query
               .filter_by(id=event.id, event_type="oauth_state_issued")
               .update({"event_type": "oauth_state_consumed"}, synchronize_session=False))
    db.session.commit()
    if updated != 1:
        raise IntegrationError("oauth_state_used", 400)
    if int(payload.get("expires") or 0) < int(time.time()):
        raise IntegrationError("oauth_state_expired", 400)
    tenant = db.session.get(TenantProfile, int(event.tenant_id))
    if tenant is None:
        raise IntegrationError("oauth_state_invalid", 400)
    return tenant, payload


def verify_shopify_hmac(query: Mapping[str, str]) -> bool:
    supplied = str(query.get("hmac") or "")
    message = "&".join(f"{k}={v}" for k, v in sorted((k, v) for k, v in query.items() if k not in {"hmac", "signature"}))
    digest = hmac.new(_cfg("SHOPIFY_CLIENT_SECRET").encode(), message.encode(), hashlib.sha256).hexdigest()
    return bool(supplied) and hmac.compare_digest(digest, supplied)


def _post(url: str, **kwargs: Any) -> dict[str, Any]:
    try:
        response = requests.post(url, timeout=HTTP_TIMEOUT, **kwargs)
    except requests.RequestException:
        raise IntegrationError("provider_unreachable", 502) from None
    if not response.ok:
        raise IntegrationError("provider_rejected_code", 502)
    try:
        return response.json()
    except ValueError:
        raise IntegrationError("provider_bad_response", 502) from None


def complete_connection(provider_key: str, query: Mapping[str, str]) -> TenantProfile:
    """Validate the callback, exchange the code and store sealed credentials."""
    item = provider(provider_key)
    if not provider_configured(item):
        raise IntegrationError("provider_not_configured", 409)
    if item.key == "shopify" and not verify_shopify_hmac(query):
        raise IntegrationError("oauth_signature_invalid", 400)
    tenant, state_payload = _consume_state(item, query.get("state", ""))
    code = str(query.get("code") or "").strip()
    if not code or len(code) > 512:
        raise IntegrationError("oauth_code_missing", 400)
    client_id, client_secret = _cfg(item.client_id_env), _cfg(item.client_secret_env)
    account_name = None
    if item.key == "mercadolibre":
        token = _post("https://api.mercadolibre.com/oauth/token", data={
            "grant_type": "authorization_code", "client_id": client_id, "client_secret": client_secret,
            "code": code, "redirect_uri": callback_url(item)}, headers={"Accept": "application/json"})
        credentials = {"access_token": token.get("access_token"), "refresh_token": token.get("refresh_token"),
                       "expires_at": int(time.time()) + int(token.get("expires_in") or 0),
                       "user_id": token.get("user_id")}
        account_name = f"Vendedor {token.get('user_id')}" if token.get("user_id") else None
    elif item.key == "tiendanube":
        token = _post("https://www.tiendanube.com/apps/authorize/token", json={
            "client_id": client_id, "client_secret": client_secret,
            "grant_type": "authorization_code", "code": code})
        credentials = {"access_token": token.get("access_token"), "store_id": token.get("user_id")}
        account_name = f"Tienda {token.get('user_id')}" if token.get("user_id") else None
    else:
        shop = normalize_shop(query.get("shop"))
        if shop != state_payload.get("shop"):
            raise IntegrationError("shopify_shop_mismatch", 400)
        token = _post(f"https://{shop}/admin/oauth/access_token", json={
            "client_id": client_id, "client_secret": client_secret, "code": code})
        credentials = {"access_token": token.get("access_token"), "shop": shop}
        account_name = shop
    if not credentials.get("access_token"):
        raise IntegrationError("provider_bad_response", 502)
    account = IntegrationAccount.query.filter_by(tenant_id=int(tenant.id), type=item.account_type).first()
    if account is None:
        account = IntegrationAccount(tenant_id=int(tenant.id), type=item.account_type, credentials={})
    account.credentials = seal(credentials)
    account.status = "active"
    account.metadata_payload = {**(account.metadata_payload or {}), "account_name": account_name,
                                "connected_at": datetime.now(timezone.utc).isoformat(),
                                "connected_by": state_payload.get("user_id")}
    db.session.add(account)
    db.session.commit()
    return tenant


def disconnect(tenant: TenantProfile, provider_key: str) -> bool:
    item = provider(provider_key)
    account = IntegrationAccount.query.filter_by(tenant_id=int(tenant.id), type=item.account_type).first()
    if account is None:
        return False
    account.credentials = {}
    account.status = "disconnected"
    db.session.add(account)
    db.session.commit()
    return True


# --- catalog import ------------------------------------------------------------

@dataclass
class RemoteProduct:
    external_id: str
    name: str
    price: Decimal | None
    stock: int | None
    sku: str | None = None
    image_url: str | None = None
    category: str | None = None
    url: str | None = None
    description: str | None = None
    active: bool = True


def _decimal(value: Any) -> Decimal | None:
    try:
        number = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _int(value: Any) -> int | None:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _localized(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("es") or value.get("pt") or next(iter(value.values()), "") or "")
    return str(value or "")


def _get(url: str, **kwargs: Any) -> requests.Response:
    try:
        response = requests.get(url, timeout=HTTP_TIMEOUT, **kwargs)
    except requests.RequestException:
        raise IntegrationError("provider_unreachable", 502) from None
    if response.status_code in {401, 403}:
        raise IntegrationError("integration_reconnect_required", 409)
    if not response.ok:
        raise IntegrationError("provider_error", 502)
    return response


def _ml_access_token(account: IntegrationAccount, credentials: dict[str, Any]) -> str:
    if int(credentials.get("expires_at") or 0) - 120 > int(time.time()):
        return str(credentials["access_token"])
    if not credentials.get("refresh_token"):
        raise IntegrationError("integration_reconnect_required", 409)
    token = _post("https://api.mercadolibre.com/oauth/token", data={
        "grant_type": "refresh_token", "client_id": _cfg("MERCADOLIBRE_CLIENT_ID"),
        "client_secret": _cfg("MERCADOLIBRE_CLIENT_SECRET"), "refresh_token": credentials["refresh_token"]},
        headers={"Accept": "application/json"})
    credentials.update(access_token=token.get("access_token"),
                       refresh_token=token.get("refresh_token") or credentials.get("refresh_token"),
                       expires_at=int(time.time()) + int(token.get("expires_in") or 0))
    account.credentials = seal(credentials)
    db.session.add(account)
    db.session.commit()
    return str(credentials["access_token"])


def _fetch_mercadolibre(account: IntegrationAccount, credentials: dict[str, Any]) -> list[RemoteProduct]:
    headers = {"Authorization": f"Bearer {_ml_access_token(account, credentials)}"}
    user_id = credentials.get("user_id")
    ids: list[str] = []
    offset = 0
    while len(ids) < IMPORT_MAX_ITEMS:
        page = _get(f"https://api.mercadolibre.com/users/{user_id}/items/search",
                    params={"status": "active", "offset": offset, "limit": 50}, headers=headers).json()
        batch = [str(value) for value in page.get("results") or []]
        ids.extend(batch)
        offset += len(batch)
        if not batch or offset >= int((page.get("paging") or {}).get("total") or 0):
            break
    products: list[RemoteProduct] = []
    for start in range(0, min(len(ids), IMPORT_MAX_ITEMS), 20):
        rows = _get("https://api.mercadolibre.com/items", params={"ids": ",".join(ids[start:start + 20])},
                    headers=headers).json()
        for row in rows if isinstance(rows, list) else []:
            body = row.get("body") if isinstance(row, Mapping) and row.get("code") == 200 else None
            if not isinstance(body, Mapping):
                continue
            sku = body.get("seller_custom_field") or next(
                (a.get("value_name") for a in body.get("attributes") or [] if a.get("id") == "SELLER_SKU"), None)
            pictures = body.get("pictures") or []
            products.append(RemoteProduct(
                external_id=str(body.get("id")), name=str(body.get("title") or "")[:255],
                price=_decimal(body.get("price")), stock=_int(body.get("available_quantity")), sku=sku,
                image_url=(pictures[0].get("secure_url") if pictures else None) or body.get("thumbnail"),
                url=body.get("permalink"), active=body.get("status") == "active"))
    return products


def _fetch_tiendanube(account: IntegrationAccount, credentials: dict[str, Any]) -> list[RemoteProduct]:
    headers = {"Authentication": f"bearer {credentials['access_token']}", "User-Agent": _USER_AGENT}
    products: list[RemoteProduct] = []
    page = 1
    while len(products) < IMPORT_MAX_ITEMS:
        rows = _get(f"https://api.tiendanube.com/v1/{credentials['store_id']}/products",
                    params={"page": page, "per_page": 50}, headers=headers).json()
        if not rows:
            break
        for row in rows:
            variant = (row.get("variants") or [{}])[0]
            images = row.get("images") or []
            categories = row.get("categories") or []
            products.append(RemoteProduct(
                external_id=str(row.get("id")), name=_localized(row.get("name"))[:255],
                price=_decimal(variant.get("promotional_price") or variant.get("price")),
                stock=_int(variant.get("stock")) if variant.get("stock_management") else None,
                sku=variant.get("sku") or None, image_url=images[0].get("src") if images else None,
                category=_localized(categories[0].get("name")) if categories else None,
                url=row.get("canonical_url"), description=None, active=bool(row.get("published", True))))
        if len(rows) < 50:
            break
        page += 1
    return products


def _fetch_shopify(account: IntegrationAccount, credentials: dict[str, Any]) -> list[RemoteProduct]:
    shop = normalize_shop(credentials.get("shop"))
    headers = {"X-Shopify-Access-Token": str(credentials["access_token"])}
    url: str | None = f"https://{shop}/admin/api/{_SHOPIFY_API_VERSION}/products.json"
    params: dict[str, Any] | None = {"limit": 50, "status": "active"}
    products: list[RemoteProduct] = []
    while url and len(products) < IMPORT_MAX_ITEMS:
        response = _get(url, params=params, headers=headers)
        for row in response.json().get("products") or []:
            variant = (row.get("variants") or [{}])[0]
            image = row.get("image") or {}
            products.append(RemoteProduct(
                external_id=str(row.get("id")), name=str(row.get("title") or "")[:255],
                price=_decimal(variant.get("price")), stock=_int(variant.get("inventory_quantity")),
                sku=variant.get("sku") or None, image_url=image.get("src"),
                category=row.get("product_type") or None, url=f"https://{shop}/products/{row.get('handle')}",
                active=row.get("status", "active") == "active"))
        next_link = response.links.get("next", {}).get("url") if hasattr(response, "links") else None
        url, params = (next_link, None) if next_link and next_link.startswith(f"https://{shop}/") else (None, None)
    return products


FETCHERS: dict[str, Callable[[IntegrationAccount, dict[str, Any]], list[RemoteProduct]]] = {
    "mercadolibre": _fetch_mercadolibre,
    "tiendanube": _fetch_tiendanube,
    "shopify": _fetch_shopify,
}


def _owner_user_id(tenant: TenantProfile) -> int:
    owner = getattr(tenant, "pyme_id", None) or getattr(tenant, "municipio_id", None)
    if not owner:
        raise IntegrationError("tenant_owner_missing", 409)
    return int(owner)


def _price_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if value == value.to_integral_value() else f"{value:.2f}"


def import_catalog(tenant: TenantProfile, provider_key: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Bring the store's products into the Chatboc catalog (or preview it)."""
    item = provider(provider_key)
    account = IntegrationAccount.query.filter_by(tenant_id=int(tenant.id), type=item.account_type,
                                                 status="active").first()
    if account is None:
        raise IntegrationError("integration_not_connected", 409)
    credentials = unseal(account.credentials)
    remote = FETCHERS[item.key](account, credentials)[:IMPORT_MAX_ITEMS]
    owner = _owner_user_id(tenant)
    now = datetime.now(timezone.utc)
    created = updated = skipped = 0
    preview: list[dict[str, Any]] = []
    for product in remote:
        if not product.name:
            skipped += 1
            continue
        sku = str(product.sku or f"{item.sku_prefix}-{product.external_id}")[:100]
        existing = CatalogoItem.query.filter_by(tenant_id=int(tenant.id), sku=sku).first()
        if len(preview) < 20:
            preview.append({"sku": sku, "nombre": product.name, "precio": _price_text(product.price),
                            "stock": product.stock, "imagen_url": product.image_url,
                            "accion": "actualizar" if existing else "crear"})
        if dry_run:
            created += existing is None
            updated += existing is not None
            continue
        row = existing or CatalogoItem(user_id=owner, tenant_id=int(tenant.id), sku=sku, nombre=product.name,
                                       modalidad="venta", precio_monetario=0.0)
        row.nombre = product.name
        row.precio = _price_text(product.price)
        if product.price is not None:
            row.precio_monetario = float(product.price)
        if product.stock is not None:
            row.cantidad = str(product.stock)
        if product.image_url:
            row.imagen_url = str(product.image_url)[:512]
        if product.category:
            row.categoria = str(product.category)[:100]
        if product.url:
            row.external_url = str(product.url)[:500]
        row.disponible = bool(product.active) and product.price is not None and product.stock != 0
        metadata = dict(row.extra_metadata or {}) if isinstance(row.extra_metadata, Mapping) else {}
        metadata["integration"] = {"provider": item.key, "external_id": product.external_id,
                                   "synced_at": now.isoformat()}
        row.extra_metadata = metadata
        db.session.add(row)
        created += existing is None
        updated += existing is not None
    if not dry_run:
        account.last_sync_at = now
        account.metadata_payload = {**(account.metadata_payload or {}),
                                    "last_import": {"created": created, "updated": updated, "skipped": skipped,
                                                    "found": len(remote), "at": now.isoformat()}}
        db.session.add(account)
        db.session.commit()
    return {"success": True, "provider": item.key, "dry_run": dry_run, "found": len(remote),
            "created": created, "updated": updated, "skipped": skipped,
            "limited": len(remote) >= IMPORT_MAX_ITEMS, "preview": preview}


def status(tenant: TenantProfile) -> dict[str, Any]:
    accounts = {row.type: row for row in IntegrationAccount.query.filter_by(tenant_id=int(tenant.id)).all()}
    rows = []
    for item in PROVIDERS.values():
        account = accounts.get(item.account_type)
        meta = account.metadata_payload if account is not None and isinstance(account.metadata_payload, Mapping) else {}
        sealed = account is not None and isinstance(account.credentials, Mapping) and account.credentials.get("v") == "1"
        rows.append({
            "key": item.key, "label": item.label, "configured": provider_configured(item),
            "connected": bool(account is not None and account.status == "active" and sealed),
            "needs_reconnect": bool(account is not None and account.status == "active" and not sealed),
            "needs_shop": item.needs_shop, "account": meta.get("account_name"),
            "last_sync_at": account.last_sync_at.isoformat() if account is not None and account.last_sync_at else None,
            "last_import": meta.get("last_import"), "capabilities": list(item.capabilities),
        })
    return {"contract_version": "tenant.marketplace_integrations.v1", "providers": rows,
            "upcoming": list(UPCOMING)}
