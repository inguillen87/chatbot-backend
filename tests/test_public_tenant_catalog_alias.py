from app import db
from models import CatalogoItem, MarketCart, MarketCartItem, MunicipioTicket, Promocion, PromocionAlcance, PymePedido, TenantFollower, TenantProfile, User


def _seed_pyme_tenant_with_catalog():
    owner = User(email="owner-pyme@test.com", name="Owner Pyme", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="tienda-demo", nombre="Tienda Demo", tipo="pyme", plan="full", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto Demo",
        categoria="general",
        precio="100",
        cantidad="10",
        marca="Marca Demo",
        disponible=True,
        promocion_info="Oferta destacada",
    )
    db.session.add(item)
    db.session.add(
        CatalogoItem(
            user_id=owner.id,
            tenant_id=tenant.id,
            nombre="Producto Premium",
            categoria="premium",
            precio="350",
            cantidad="4",
            marca="Marca Premium",
            disponible=True,
        )
    )
    db.session.commit()

    return tenant


def _seed_municipio_tenant_with_catalog():
    owner = User(email="mauricio@junin.test", name="Municipio de Junin", rol="admin", tipo_chat="municipio")
    owner.set_password("123456")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(
        slug="municipio",
        nombre="Municipio de Junin",
        tipo="municipio",
        plan="full",
        municipio_id=owner.id,
        vertical="gobierno",
    )
    db.session.add(tenant)
    db.session.commit()

    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Canje puntos verdes",
        categoria="Participacion ciudadana",
        precio="800 pts",
        cantidad="100",
        disponible=True,
        modalidad="canje",
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
    assert all(item.get("tenant_id") for item in payload)
    assert all(item.get("tenant_slug") for item in payload)
    assert all(item.get("catalogo_item_id") == item.get("catalog_item_id") for item in payload)


def test_public_catalog_alias_prefers_query_tenant_slug_over_type_alias(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get("/api/public/tenants/pyme/catalog?tenant_slug=tienda-demo")
    assert resp.status_code == 200

    payload = resp.get_json()
    assert isinstance(payload, list)
    assert len(payload) >= 1
    assert all(item["tenant_id"] == tenant.id for item in payload)
    assert all(item["tenant_slug"] == tenant.slug for item in payload)
    assert all(item["catalogo_item_id"] == item["catalog_item_id"] for item in payload)


def test_public_market_catalog_contract_includes_promotions(client):
    tenant = _seed_pyme_tenant_with_catalog()
    owner = tenant.pyme
    promo = Promocion(
        pyme_user_id=owner.id,
        nombre_promocion="15% lanzamiento",
        descripcion_publica="Descuento para primeros pedidos.",
        tipo_promocion="PORCENTAJE_CATEGORIA",
        valor_descuento=15,
        monto_minimo_carrito=5000,
        is_active=True,
    )
    db.session.add(promo)
    db.session.flush()
    db.session.add(
        PromocionAlcance(
            promocion_id=promo.id,
            tipo_alcance="CATEGORIA",
            nombre_categoria="general",
        )
    )
    db.session.commit()

    resp = client.get(f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["contract_version"] == "public.market_catalog.v1"
    assert payload["tenant_slug"] == tenant.slug
    assert len(payload["products"]) >= 1
    assert payload["promotions"]["contract_version"] == "public.catalog_promotions.v1"
    assert payload["promotions"]["enabled"] is True
    assert payload["promotions"]["items"][0]["title"] == "15% lanzamiento"
    assert payload["promotions"]["items"][0]["display_badge"] == "15% OFF"
    assert payload["promotions"]["items"][0]["status"] == "active_now"
    assert payload["promotions"]["items"][0]["eligible_product_ids"] == [payload["products"][0]["catalogo_item_id"]]
    assert payload["promotions"]["items"][0]["alcances"][0]["tipo_alcance"] == "CATEGORIA"
    assert payload["promotions"]["items"][0]["alcances"][0]["nombre_categoria"] == "general"
    assert payload["facets"]["contract_version"] == "public.market_catalog_facets.v1"
    assert any(item["value"] == "general" for item in payload["facets"]["categories"])
    assert any(item["value"] == "Marca Demo" for item in payload["facets"]["brands"])
    assert payload["facets"]["promotion_count"] >= 1
    assert payload["assisted_intake"]["contract_version"] == "marketplace.assisted_intake_entry.v1"
    assert payload["assisted_intake"]["mode"] == "catalog_plus_assisted"
    assert payload["assisted_intake"]["anonymous_intake"] is True
    assert payload["assisted_intake"]["submit"]["contract_version"] == "marketplace.assisted_intake_submit.v1"
    assert payload["assisted_intake"]["submit"]["endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert payload["assisted_intake"]["submit"]["file_field"] == "archivo"
    assert payload["assisted_intake"]["submit"]["text_field"] == "pedido_text"
    assert "image/jpeg" in payload["assisted_intake"]["submit"]["accepted_mime_types"]
    assert "service_request" in {item["id"] for item in payload["assisted_intake"]["document_types"]}
    assert payload["assisted_intake"]["frontend_contract"]["show_quick_examples"] is True
    assert payload["assisted_intake"]["frontend_contract"]["submit_endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert payload["assisted_intake"]["frontend_contract"]["max_file_mb"] == 8
    assert any(example["document_type"] == "quote_request" for example in payload["assisted_intake"]["text_examples"])
    assert payload["public_api"]["contract_version"] == "marketplace.public_api.v1"
    assert payload["public_api"]["anonymous"] is True
    assert payload["public_api"]["catalog"]["endpoint"] == f"/api/market/{tenant.slug}/catalog?contract=marketplace"
    assert payload["public_api"]["catalog"]["alias_endpoint"] == f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace"
    assert payload["public_api"]["cart"]["add"]["endpoint"] == f"/api/market/{tenant.slug}/cart/add"
    assert payload["public_api"]["checkout"]["start"]["endpoint"] == f"/api/market/{tenant.slug}/checkout/start"
    assert payload["public_api"]["checkout"]["fallback_behavior"] == "return_structured_plan_or_payment_error_never_tokenized_endpoint"
    assert payload["public_api"]["assisted_upload"]["endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert payload["public_api"]["tracking"]["order_path_template"] == f"/tracking/order/{{code}}?tenant_slug={tenant.slug}"
    assert payload["frontend_contract"]["show_promotions_strip"] is True
    assert payload["frontend_contract"]["show_faceted_filters"] is True
    assert payload["frontend_contract"]["show_assisted_intake"] is True
    assert payload["frontend_contract"]["public_api_contract"] == "marketplace.public_api.v1"
    assert payload["frontend_contract"]["use_public_api_endpoints"] is True


def test_public_market_catalog_contract_for_empty_catalog_promotes_assisted_intake(client):
    owner = User(email="owner-empty-market@test.com", name="Owner Empty", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.commit()

    tenant = TenantProfile(slug="empty-market", nombre="Empty Market", tipo="pyme", plan="full", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    resp = client.get(f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace")

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["products"] == []
    assert payload["total"] == 0
    assert payload["heroSubtitle"] == "Marketplace asistido para pedidos, boletas y documentos sin registro."
    assert payload["assisted_intake"]["mode"] == "assisted_first"
    assert payload["assisted_intake"]["show_on_empty_catalog"] is True
    assert payload["assisted_intake"]["empty_state"]["primary_cta"] == "Subir pedido o documento"
    assert payload["frontend_contract"]["empty_catalog_mode"] == "assisted_first"


def test_market_catalog_contract_matches_public_catalog_contract_shape(client):
    tenant = _seed_pyme_tenant_with_catalog()

    public_resp = client.get(f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace")
    market_resp = client.get(f"/api/market/{tenant.slug}/catalog?contract=marketplace")

    assert public_resp.status_code == 200
    assert market_resp.status_code == 200
    public_payload = public_resp.get_json()
    market_payload = market_resp.get_json()
    for payload in (public_payload, market_payload):
        assert payload["contract_version"] == "public.market_catalog.v1"
        assert payload["tenant_slug"] == tenant.slug
        assert payload["products"]
        assert payload["facets"]["categories"]
        assert payload["promotions"]["contract_version"] == "public.catalog_promotions.v1"
        assert payload["frontend_contract"]["render_as"] == "marketplace_catalog"
        assert payload["public_api"]["contract_version"] == "marketplace.public_api.v1"
        assert payload["public_api"]["cart"]["summary"]["endpoint"] == f"/api/market/{tenant.slug}/cart"
    assert set(public_payload.keys()) == set(market_payload.keys())


def test_municipio_market_catalog_contract_exposes_service_request_intake(client):
    tenant = _seed_municipio_tenant_with_catalog()

    resp = client.get(f"/api/market/{tenant.slug}/catalog?contract=marketplace")

    assert resp.status_code == 200
    payload = resp.get_json()
    assisted = payload["assisted_intake"]
    assert "municipio" in assisted["title"].lower()
    assert any(item["id"] == "service_request" for item in assisted["document_types"])
    assert any(example["document_type"] == "service_request" for example in assisted["text_examples"])
    assert "boleta" in assisted["empty_state"]["description"].lower()


def test_market_catalog_contract_filters_promotions_and_price_range(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get(
        f"/api/market/{tenant.slug}/catalog?contract=marketplace&en_promocion=true&precio_max=150&sort=promo_first"
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 1
    assert payload["products"][0]["nombre"] == "Producto Demo"
    assert payload["filters"]["en_promocion"] is True
    assert payload["filters"]["precio_max"] == 150.0
    assert payload["sort_options"][1]["id"] == "promo_first"


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
        assert body["reason_code"] == "reserved_public_slug"
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


def test_public_domain_slug_alias_resolves_navigation_commerce_and_history(client):
    tenant = _seed_pyme_tenant_with_catalog()
    tenant.dominio = f"{tenant.slug}.chatboc.ar"
    db.session.add(tenant)
    db.session.commit()

    domain_slug = f"{tenant.slug}.chatboc.ar"

    nav_resp = client.get(f"/api/public/tenants/{domain_slug}/public-navigation")
    assert nav_resp.status_code == 200
    assert nav_resp.get_json()["tenant_slug"] == tenant.slug

    commerce_resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": domain_slug, "tenant": domain_slug},
        headers={"X-Chat-Session-Id": "chat_domain_slug", "X-Anon-Id": "anon_domain_slug"},
    )
    assert commerce_resp.status_code == 200
    commerce = commerce_resp.get_json()
    assert commerce["tenant"]["slug"] == tenant.slug
    assert commerce["catalog"]["endpoint"] == f"/api/public/tenants/{tenant.slug}/catalog"

    history_resp = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": domain_slug, "tenant": domain_slug},
        headers={"X-Chat-Session-Id": "chat_domain_slug", "X-Anon-Id": "anon_domain_slug"},
    )
    assert history_resp.status_code == 200
    assert history_resp.get_json()["tenant_slug"] == tenant.slug


def test_explicit_domain_slug_wins_over_stale_widget_token(client):
    tenant = _seed_pyme_tenant_with_catalog()
    tenant.dominio = f"{tenant.slug}.chatboc.ar"

    stale_owner = User(email="stale-owner@test.com", name="Stale Owner", rol="admin", tipo_chat="municipio")
    stale_owner.set_password("pass")
    db.session.add(stale_owner)
    db.session.flush()
    stale_tenant = TenantProfile(
        slug="stale-municipio",
        nombre="Stale Municipio",
        tipo="municipio",
        plan="full",
        municipio_id=stale_owner.id,
        configuracion={"widget_tokens": ["stale-token"]},
    )
    db.session.add_all([tenant, stale_tenant])
    db.session.commit()

    domain_slug = f"{tenant.slug}.chatboc.ar"
    resp = client.get(
        "/api/pwa/public/cart/summary",
        query_string={"tenant_slug": domain_slug, "tenant": domain_slug, "widget_token": "stale-token"},
        headers={"X-Chat-Session-Id": "chat_stale_token", "X-Anon-Id": "anon_stale_token"},
    )

    assert resp.status_code == 200
    db.session.refresh(tenant)
    assert "stale-token" not in str(tenant.configuracion or "")


def test_public_widget_referrer_tenant_wins_over_stale_query_slug(client):
    tenant = _seed_pyme_tenant_with_catalog()
    referer = f"https://www.chatboc.ar/t/{tenant.slug}.chatboc.ar"

    commerce_resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": "municipio", "tenant": "municipio"},
        headers={"Referer": referer, "X-Chat-Session-Id": "chat_referrer", "X-Anon-Id": "anon_referrer"},
    )
    assert commerce_resp.status_code == 200
    assert commerce_resp.get_json()["tenant"]["slug"] == tenant.slug

    cart_resp = client.get(
        "/api/pwa/public/cart/summary",
        query_string={"tenant_slug": "municipio", "tenant": "municipio"},
        headers={"Referer": referer, "X-Chat-Session-Id": "chat_referrer", "X-Anon-Id": "anon_referrer"},
    )
    assert cart_resp.status_code == 200


def test_reserved_public_slug_navigation_returns_reserved_json(client):
    resp = client.get("/api/public/tenants/precios/public-navigation", headers={"Origin": "https://www.chatboc.ar"})

    assert resp.status_code == 404
    body = resp.get_json()
    assert body["contract_version"] == "public.reserved_slug.v1"
    assert body["reason_code"] == "reserved_public_slug"
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
    assert body["cart"]["checkout_preview_endpoint"] == "/api/pwa/public/cart/summary"
    assert body["cart"]["checkout_session_endpoint"] == "/api/checkout/crear-preferencia"
    assert body["cart"]["allow_guest_cart"] is True
    assert body["payment"]["contract_version"] == "commerce.conversational_checkout_experience.v1"
    assert body["payment"]["ready"] is False
    assert body["payment"]["reason_code"] == "payment_gateway_not_configured"
    assert body["payment"]["blocking_reasons"][0]["id"] == "payment_gateway_not_configured"
    assert body["payment"]["operator_next_actions"][1]["id"] == "connect_gateway"
    assert body["payment"]["operator_next_actions"][1]["status"] == "required"
    assert body["payment"]["policy"]["confirmation_source"] == "server_to_server_webhook"
    assert body["portal"]["enabled"] is True
    assert body["portal"]["label"] == "Mi actividad"
    assert body["portal"]["view_url"] == f"/portal/{tenant.slug}"
    assert body["portal"]["history_endpoint"] == "/api/public/widget-user/tenant-history"
    assert body["portal"]["scope"] == "end_user_tenant_history"
    assert "portal" in body["frontend_contract"]["primary_actions"]
    assert "checkout" in body["frontend_contract"]["primary_actions"]
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


def test_widget_commerce_session_requires_full_plan(client):
    tenant = _seed_pyme_tenant_with_catalog()
    tenant.plan = "free"
    db.session.add(tenant)
    db.session.commit()

    resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": tenant.slug},
        headers={
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "chat_public_locked",
            "X-Anon-Id": "anon_public_locked",
        },
    )

    assert resp.status_code == 403
    body = resp.get_json()
    assert body["error"] == "plan_required"
    assert body["reason_code"] == "plan_full_required"
    assert body["access"]["enabled"] is False
    assert body["frontend_contract"]["render_as"] == "integration_locked"
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"


def test_municipio_widget_external_contract_enables_portal_and_real_catalog_cart(client):
    tenant = _seed_municipio_tenant_with_catalog()

    resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": tenant.slug, "tenant": tenant.slug},
        headers={
            "Origin": "https://external-widget-test.local",
            "X-Chat-Session-Id": "chat_junin_external",
            "X-Anon-Id": "anon_junin_external",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["contract_version"] == "public.widget_commerce_session.v1"
    assert body["tenant"]["slug"] == tenant.slug
    assert body["tenant"]["vertical"] == "gobierno"
    assert body["catalog"]["enabled"] is True
    assert body["cart"]["enabled"] is True
    assert body["portal"]["enabled"] is True
    assert body["portal"]["view_url"] == f"/portal/{tenant.slug}"
    assert "portal" in body["frontend_contract"]["primary_actions"]
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://external-widget-test.local"


def test_reserved_media_slug_widget_endpoints_degrade_to_json(client):
    for path in (
        "/api/public/widget-commerce-session?tenant_slug=media&tenant=media",
        "/api/public/widget-user/tenant-history?tenant_slug=media&tenant=media",
        "/api/public/tenants/media/widget-config?tenant_slug=media&tenant=media",
    ):
        resp = client.get(path, headers={"Origin": "https://www.chatboc.ar"})
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["contract_version"] == "public.reserved_slug.v1"
        assert body["reason_code"] == "reserved_public_slug"
        assert body["reserved_slug"] == "media"
        assert body["slug"] == "media"
        assert body["request_id"]
        assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
        assert resp.headers.get("X-Request-Id")


def test_media_reserved_slug_does_not_bootstrap_tenant_widget(client):
    test_reserved_media_slug_widget_endpoints_degrade_to_json(client)


def test_demo_catalogs_reserved_slug_does_not_bootstrap_widget_config(client):
    resp = client.get(
        "/api/public/tenants/demo-catalogs/widget-config?tenant_slug=demo-catalogs&tenant=demo-catalogs",
        headers={"Origin": "https://www.chatboc.ar"},
    )

    assert resp.status_code == 404
    body = resp.get_json()
    assert body["contract_version"] == "public.reserved_slug.v1"
    assert body["reason_code"] == "reserved_public_slug"
    assert body["slug"] == "demo-catalogs"
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"


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


def test_widget_user_register_links_municipio_anon_history_cart_and_follower(client):
    tenant = _seed_municipio_tenant_with_catalog()
    owner = tenant.municipio
    cart = MarketCart(tenant_id=tenant.id, session_id="chat_public_muni", status="open")
    db.session.add(cart)
    db.session.flush()
    item = CatalogoItem.query.filter_by(tenant_id=tenant.id).first()
    db.session.add(
        MarketCartItem(
            cart_id=cart.id,
            product_id=item.id,
            quantity=1,
            name_snapshot=item.nombre,
            price_text=item.precio,
        )
    )
    db.session.add(
        MunicipioTicket(
            pregunta="Luminaria rota en la esquina",
            asunto="Alumbrado publico",
            categoria="alumbrado",
            municipio_id=owner.id,
            tenant_id=tenant.id,
            anon_id="anon_public_muni",
            canal_ingreso="widget",
        )
    )
    db.session.commit()

    register_resp = client.post(
        "/api/public/widget-user/register",
        query_string={"tenant_slug": tenant.slug},
        json={"name": "Vecino Junin", "phone": "+5492610000000"},
        headers={
            "Origin": "https://external-widget-test.local",
            "X-Chat-Session-Id": "chat_public_muni",
            "X-Anon-Id": "anon_public_muni",
        },
    )

    assert register_resp.status_code == 200
    body = register_resp.get_json()
    assert body["contract_version"] == "public.widget_user_register.v1"
    assert body["status"] == "registered"
    assert body["tenant_slug"] == tenant.slug
    assert body["profile"]["is_registered"] is True
    assert body["tenant_follow"]["linked"] is True
    assert body["merge"]["municipio_tickets"] == 1
    assert body["merge"]["market_carts"] == 1

    user_id = body["profile"]["user_id"]
    assert TenantFollower.query.filter_by(user_id=user_id, tenant_id=tenant.id).first() is not None
    assert MarketCart.query.filter_by(session_id="chat_public_muni").first().user_id == user_id
    assert MunicipioTicket.query.filter_by(asunto="Alumbrado publico").first().user_id == user_id

    history_resp = client.get(
        "/api/public/widget-user/tenant-history",
        query_string={"tenant_slug": tenant.slug},
        headers={"X-Chat-Session-Id": "chat_public_muni", "X-Anon-Id": "anon_public_muni"},
    )
    history = history_resp.get_json()
    assert history["cart"]["items_count"] == 1
    assert "claim" in {entry["kind"] for entry in history["items"]}
