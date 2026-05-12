from app import db
from models import CatalogoItem, MarketCart, MarketCartItem, MunicipioTicket, PymePedido, TenantProfile, User


def _seed_pyme_tenant_with_catalog():
    owner = User(email="owner-pyme@test.com", name="Owner Pyme", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tienda-demo", nombre="Tienda Demo", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto Demo",
        categoria="general",
        precio="100",
        cantidad="10",
        disponible=True,
    )
    db.session.add(item)
    db.session.commit()

    return tenant


def test_public_catalog_alias_slug_pyme_uses_active_pyme_tenant(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get("/api/public/tenants/pyme/catalog")
    assert resp.status_code == 200

    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_public_catalog_alias_prefers_query_tenant_slug_over_type_alias(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get("/api/public/tenants/pyme/catalog?tenant_slug=tienda-demo")
    assert resp.status_code == 200

    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_public_catalog_legacy_alias_without_api_prefix(client):
    _seed_pyme_tenant_with_catalog()

    resp = client.get("/public/tenants/pyme/catalog")
    assert resp.status_code == 200

    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_public_catalog_alias_single_letter_e_maps_to_pyme(client):
    _seed_pyme_tenant_with_catalog()

    resp = client.get("/api/public/tenants/e/catalog")
    assert resp.status_code == 200

    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) >= 1


def test_public_catalog_options_allows_x_token_header(client):
    resp = client.options(
        "/api/public/tenants/pyme/catalog",
        headers={
            "Origin": "https://www.chatboc.ar",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "x-token",
        },
    )

    assert resp.status_code == 200
    allow_headers = resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "x-token" in allow_headers


def test_reserved_public_slug_catalog_degrades_to_json(client):
    for path in (
        "/api/public/tenants/casos/catalog?tenant_slug=casos&tenant=casos",
        "/public/tenants/casos/catalog?tenant_slug=casos&tenant=casos",
    ):
        resp = client.get(path, headers={"Origin": "https://www.chatboc.ar"})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["contract_version"] == "public.catalog_resolution.v1"
        assert body["ok"] is False
        assert body["reason_code"] == "reserved_public_route"
        assert body["items"] == []
        assert body["cart"]["enabled"] is False
        assert body["request_id"]
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"


def test_public_navigation_contract_disables_unavailable_items(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get(f"/api/public/tenants/{tenant.slug}/public-navigation")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["contract_version"] == "tenant.public_navigation.v1"
    assert body["tenant_slug"] == tenant.slug
    items = {item["id"]: item for item in body["items"]}
    assert items["home"]["enabled"] is True
    assert items["catalog"]["enabled"] is True
    assert items["news"]["enabled"] is False
    assert items["news"]["empty_state"]


def test_reserved_public_slug_navigation_returns_reserved_json(client):
    resp = client.get("/api/public/tenants/precios/public-navigation", headers={"Origin": "https://www.chatboc.ar"})

    assert resp.status_code == 404
    body = resp.get_json()
    assert body["contract_version"] == "public.reserved_slug.v1"
    assert body["reason_code"] == "reserved_public_route"
    assert body["request_id"]
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"


def test_widget_commerce_session_returns_embedded_operating_contract(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": tenant.slug},
        headers={
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "chat_public_1",
            "X-Anon-Id": "anon_public_1",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["contract_version"] == "public.widget_commerce_session.v1"
    assert body["tenant"]["slug"] == tenant.slug
    assert body["session"]["chat_session_id"] == "chat_public_1"
    assert body["session"]["anon_id"] == "anon_public_1"
    assert body["session"]["widget_session_token"].startswith("wst_")
    assert body["catalog"]["endpoint"] == f"/api/public/tenants/{tenant.slug}/catalog"
    assert body["cart"]["summary_endpoint"] == "/api/pwa/public/cart/summary"
    assert body["cart"]["items_endpoint"] == "/api/pwa/public/cart/items"
    assert body["cart"]["legacy_endpoint"] == "/api/pwa/public/cart"
    assert body["cart"]["allow_guest_cart"] is True
    assert body["portal"]["history_endpoint"] == "/api/public/widget-user/tenant-history"
    assert body["accessibility"]["enabled"] is True
    assert body["accessibility"]["allow_dyslexia_mode"] is True
    assert body["accessibility"]["allow_high_contrast"] is True
    assert body["accessibility"]["allow_large_controls"] is True
    assert body["accessibility"]["captions_enabled"] is True
    assert body["accessibility"]["respect_prefers_reduced_motion"] is True
    assert body["accessibility"]["touch_target_min_px"] == 44
    assert body["frontend_contract"]["render_as"] == "embedded_tenant_operating_widget"
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
    assert resp.headers.get("X-Request-Id")


def test_widget_user_tenant_history_returns_cart_claims_and_orders(client):
    tenant = _seed_pyme_tenant_with_catalog()
    owner = tenant.pyme

    cart = MarketCart(tenant_id=tenant.id, session_id="chat_public_2", status="open")
    db.session.add(cart)
    db.session.flush()
    item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
    db.session.add(
        MarketCartItem(
            cart_id=cart.id,
            product_id=item.id,
            quantity=2,
            name_snapshot=item.nombre,
            price_text=item.precio,
        )
    )
    ticket = MunicipioTicket(
        pregunta="Necesito seguimiento",
        asunto="Reclamo demo",
        categoria="servicio",
        municipio_id=owner.id,
        tenant_id=tenant.id,
        anon_id="anon_public_2",
        canal_ingreso="widget",
    )
    pedido = PymePedido(
        pyme_id=owner.id,
        asunto="Pedido web",
        detalles="{}",
        monto_total=100.0,
        tenant_id=tenant.id,
        nombre_cliente="Cliente Demo",
    )
    db.session.add_all([ticket, pedido])
    db.session.commit()

    resp = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant.slug},
        headers={"X-Chat-Session-Id": "chat_public_2", "X-Anon-Id": "anon_public_2"},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["contract_version"] == "public.widget_user_tenant_history.v1"
    assert body["tenant_slug"] == tenant.slug
    assert body["profile"]["can_register"] is True
    assert body["cart"]["items_count"] == 2
    assert body["cart"]["summary_endpoint"] == "/api/pwa/public/cart/summary"
    assert body["cart"]["items_endpoint"] == "/api/pwa/public/cart/items"
    kinds = {item["kind"] for item in body["items"]}
    assert "claim" in kinds
    assert "order" in kinds


def test_pwa_public_cart_summary_and_items_aliases(client):
    tenant = _seed_pyme_tenant_with_catalog()

    for path in ("/api/pwa/public/cart/summary", "/api/pwa/public/cart/items"):
        resp = client.get(
            path,
            query_string={"tenant": tenant.slug},
            headers={"Origin": "https://www.chatboc.ar", "X-Chat-Session-Id": "chat_public_alias"},
        )

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["items_count"] == 0
        assert body["items"] == []
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"


def test_widget_user_register_and_link_session_are_degradable_json(client):
    tenant = _seed_pyme_tenant_with_catalog()

    register_resp = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant.slug},
        json={"name": "Cliente Demo", "email": "cliente@test.com"},
        headers={"X-Chat-Session-Id": "chat_public_3", "X-Anon-Id": "anon_public_3"},
    )
    assert register_resp.status_code == 200
    assert register_resp.get_json()["contract_version"] == "public.widget_user_register.v1"

    link_resp = client.post(
        "/api/public/widget-user/link-session",
        query_string={"tenant_slug": tenant.slug},
        headers={"X-Chat-Session-Id": "chat_public_3", "X-Anon-Id": "anon_public_3"},
    )
    assert link_resp.status_code == 200
    body = link_resp.get_json()
    assert body["contract_version"] == "public.widget_user_link_session.v1"
    assert body["linked"] is True
