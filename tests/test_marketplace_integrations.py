"""Secure marketplace connections: signed one-time state, sealed tokens, real import."""
from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from app import db
from models import CatalogoItem, IntegrationAccount
from services.integrations import marketplace_oauth as market
from tests.test_integration_fixes import _create_admin_tenant, app, client  # noqa: F401

KEY = "k" * 40


@pytest.fixture(autouse=True)
def _configured(app):  # noqa: F811
    app.config.update(INTEGRATIONS_OAUTH_KEY_V1=KEY, MERCADOLIBRE_CLIENT_ID="ml-id", MERCADOLIBRE_CLIENT_SECRET="ml-secret",
                      TIENDANUBE_APP_ID="1234", TIENDANUBE_CLIENT_SECRET="tn-secret",
                      SHOPIFY_CLIENT_ID="sh-id", SHOPIFY_CLIENT_SECRET="sh-secret")


def _ok(payload, links=None):
    response = MagicMock(ok=True, status_code=200)
    response.json.return_value = payload
    response.links = links or {}
    return response


def _state(url):
    return parse_qs(urlparse(url).query)["state"][0]


def test_status_lists_providers_and_upcoming(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    body = client.get(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces", headers=headers).get_json()
    assert [row["key"] for row in body["providers"]] == ["mercadolibre", "tiendanube", "shopify"]
    assert all(row["configured"] and not row["connected"] for row in body["providers"])
    assert [row["key"] for row in body["upcoming"]] == ["woocommerce", "amazon"]


def test_without_key_nothing_can_connect(client, app):  # noqa: F811
    app.config["INTEGRATIONS_OAUTH_KEY_V1"] = "short"
    _, tenant, headers = _create_admin_tenant()
    response = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/mercadolibre/connect", headers=headers)
    assert response.status_code == 409 and response.get_json()["error"] == "provider_not_configured"


def test_plan_lock_applies(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant(plan="free")
    response = client.get(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces", headers=headers)
    assert response.status_code == 403


def test_mercadolibre_connect_is_one_time_and_tokens_are_sealed(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    url = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/mercadolibre/connect",
                      headers=headers).get_json()["authorize_url"]
    assert url.startswith("https://auth.mercadolibre.com.ar/authorization?")
    state = _state(url)
    token = {"access_token": "APP_USR-secret", "refresh_token": "TG-r", "expires_in": 21600, "user_id": 777}
    with patch.object(market.requests, "post", return_value=_ok(token)) as post:
        response = client.get(f"/api/integrations/oauth/mercadolibre/callback?code=TG-code&state={state}")
    assert response.status_code == 302 and response.headers["Location"].endswith(
        f"/t/{tenant.slug}/integracion?channel=mercadolibre&conectado=1")
    assert post.call_args.kwargs["data"]["code"] == "TG-code"
    account = IntegrationAccount.query.filter_by(tenant_id=tenant.id, type="MercadoLibre").one()
    assert "APP_USR-secret" not in str(account.credentials)
    assert market.unseal(account.credentials)["access_token"] == "APP_USR-secret"
    with patch.object(market.requests, "post", return_value=_ok(token)) as post:
        replay = client.get(f"/api/integrations/oauth/mercadolibre/callback?code=TG-code&state={state}")
    assert replay.status_code == 400 and replay.get_json()["error"] == "oauth_state_used" and not post.called


def test_tampered_or_cross_provider_state_is_rejected(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    url = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/tiendanube/connect",
                      headers=headers).get_json()["authorize_url"]
    assert url.startswith("https://www.tiendanube.com/apps/1234/authorize?")
    nonce, signature = _state(url).split(".")
    with patch.object(market.requests, "post") as post:
        bad = client.get(f"/api/integrations/oauth/tiendanube/callback?code=x&state={nonce}.{signature[:-2]}AA")
        other = client.get(f"/api/integrations/oauth/mercadolibre/callback?code=x&state={nonce}.{signature}")
    assert bad.status_code == 400 and other.status_code == 400 and not post.called


def test_shopify_requires_valid_hmac_and_same_shop(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    url = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/shopify/connect",
                      headers=headers, json={"shop": "Mi-Tienda"}).get_json()["authorize_url"]
    assert url.startswith("https://mi-tienda.myshopify.com/admin/oauth/authorize?")
    query = {"code": "c1", "shop": "mi-tienda.myshopify.com", "state": _state(url), "timestamp": "1"}
    message = "&".join(f"{k}={v}" for k, v in sorted(query.items()))
    good = hmac.new(b"sh-secret", message.encode(), hashlib.sha256).hexdigest()
    with patch.object(market.requests, "post", return_value=_ok({"access_token": "shpat"})):
        forged = client.get("/api/integrations/oauth/shopify/callback", query_string={**query, "hmac": "0" * 64})
        assert forged.status_code == 400
        ok = client.get("/api/integrations/oauth/shopify/callback", query_string={**query, "hmac": good})
    assert ok.status_code == 302


def test_bad_shop_domain_is_refused():
    for shop in ["evil.com", "a.myshopify.com.evil.com", "", "tienda con espacios"]:
        with pytest.raises(market.IntegrationError):
            market.normalize_shop(shop)


def test_tiendanube_import_upserts_by_sku_with_price_stock_and_photo(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    db.session.add(IntegrationAccount(tenant_id=tenant.id, type="TiendaNube", status="active",
                                      credentials=market.seal({"access_token": "t", "store_id": 55})))
    db.session.add(CatalogoItem(user_id=tenant.pyme_id, tenant_id=tenant.id, sku="YER-1", nombre="Yerba vieja",
                                precio="100", cantidad="1", modalidad="venta", precio_monetario=100))
    db.session.commit()
    rows = [
        {"id": 1, "name": {"es": "Yerba mate 1 kg"}, "published": True, "canonical_url": "https://t/y",
         "variants": [{"price": "5200.00", "stock": 48, "stock_management": True, "sku": "YER-1"}],
         "images": [{"src": "https://img/y.jpg"}], "categories": [{"name": {"es": "Almacén"}}]},
        {"id": 2, "name": {"es": "Taza"}, "published": True,
         "variants": [{"price": "3500", "stock": None, "stock_management": False, "sku": ""}], "images": []},
    ]
    with patch.object(market.requests, "get", return_value=_ok(rows)) as get:
        preview = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/tiendanube/import",
                              headers=headers, json={"dry_run": True}).get_json()
        assert preview["created"] == 1 and preview["updated"] == 1
        assert CatalogoItem.query.filter_by(tenant_id=tenant.id).count() == 1
        result = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/tiendanube/import",
                             headers=headers).get_json()
    assert get.call_args.kwargs["headers"]["Authentication"] == "bearer t"
    assert result["found"] == 2 and result["created"] == 1 and result["updated"] == 1
    yerba = CatalogoItem.query.filter_by(tenant_id=tenant.id, sku="YER-1").one()
    assert (yerba.nombre, yerba.precio, yerba.cantidad, yerba.imagen_url, yerba.categoria) == (
        "Yerba mate 1 kg", "5200", "48", "https://img/y.jpg", "Almacén")
    assert yerba.extra_metadata["integration"]["provider"] == "tiendanube"
    taza = CatalogoItem.query.filter_by(tenant_id=tenant.id, sku="TN-2").one()
    assert taza.precio == "3500" and taza.disponible is True


def test_reconnect_needed_when_store_revokes_access(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant()
    db.session.add(IntegrationAccount(tenant_id=tenant.id, type="Shopify", status="active",
                                      credentials=market.seal({"access_token": "x", "shop": "a.myshopify.com"})))
    db.session.commit()
    denied = MagicMock(ok=False, status_code=401)
    with patch.object(market.requests, "get", return_value=denied):
        response = client.post(f"/api/admin/tenants/{tenant.slug}/integrations/marketplaces/shopify/import", headers=headers)
    assert response.status_code == 409 and response.get_json()["error"] == "integration_reconnect_required"


def test_store_whatsapp_flow_is_built_from_the_published_catalog(client):  # noqa: F811
    owner, tenant, headers = _create_admin_tenant(plan="free")
    empty = client.get(f"/api/admin/tenants/{tenant.slug}/marketplace/whatsapp-flow", headers=headers)
    assert empty.status_code == 409 and empty.get_json()["error"] == "catalog_without_offers"
    db.session.add_all([
        CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Kit escolar", categoria="Educación",
                     modalidad="canje", precio_puntos=1500, disponible=True),
        CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Rifa solidaria", categoria="Eventos",
                     precio="500", disponible=True),
    ])
    db.session.commit()
    body = client.get(f"/api/admin/tenants/{tenant.slug}/marketplace/whatsapp-flow", headers=headers).get_json()
    assert body["contract_version"] == "chatboc.marketplace_whatsapp_flow.v1" and body["screen"] == "TIENDA"
    assert [offer["label"] for offer in body["summary"]["offers"]] == ["1.500 puntos", "$ 500"]
    assert body["flow_json"]["screens"][0]["id"] == "TIENDA"


def test_benefits_template_loads_once_and_feeds_the_whatsapp_store(client):  # noqa: F811
    _, tenant, headers = _create_admin_tenant(plan="free")
    listed = client.get(f"/api/admin/tenants/{tenant.slug}/marketplace/templates", headers=headers).get_json()
    assert listed["templates"][0]["key"] == "beneficios_ciudadanos"
    first = client.post(f"/api/admin/tenants/{tenant.slug}/marketplace/templates/beneficios_ciudadanos/apply",
                        headers=headers).get_json()
    assert first["created"] == first["items"] == 20 and first["updated"] == 0
    again = client.post(f"/api/admin/tenants/{tenant.slug}/marketplace/templates/beneficios_ciudadanos/apply",
                        headers=headers).get_json()
    assert again["created"] == 0 and again["updated"] == 20
    assert CatalogoItem.query.filter_by(tenant_id=tenant.id).count() == 20
    missing = client.post(f"/api/admin/tenants/{tenant.slug}/marketplace/templates/nope/apply", headers=headers)
    assert missing.status_code == 404
    flow = client.get(f"/api/admin/tenants/{tenant.slug}/marketplace/whatsapp-flow", headers=headers).get_json()
    labels = {offer["name"]: offer["label"] for offer in flow["summary"]["offers"]}
    assert labels["Kit escolar completo"] == "1.500 puntos"
    assert labels["Charla: primeros años de crianza"] == "Gratis · inscripción"
    assert labels["Donar útiles escolares"] == "Donación"
    assert labels["Rifa solidaria del hospital"] == "$ 2.000"
    texts = [child.get("text") for child in flow["flow_json"]["screens"][0]["layout"]["children"]]
    assert any("canjear tus puntos, anotarte o donar" in (text or "") for text in texts)


def test_admin_catalog_lists_redemptions_and_donations_as_such(client):  # noqa: F811
    owner, tenant, headers = _create_admin_tenant(plan="free")
    db.session.add_all([
        CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Kit escolar", categoria="Educación",
                     modalidad="canje", precio_puntos=1500, disponible=True),
        CatalogoItem(user_id=owner.id, tenant_id=tenant.id, nombre="Árbol", categoria="Ambiente",
                     modalidad="donacion", disponible=True),
    ])
    db.session.commit()
    rows = {row["nombre"]: row for row in
            client.get(f"/api/admin/tenants/{tenant.slug}/catalog/items", headers=headers).get_json()}
    assert rows["Kit escolar"]["modalidad"] == "canje" and rows["Kit escolar"]["precio_puntos"] == 1500
    assert rows["Árbol"]["modalidad"] == "donacion"
