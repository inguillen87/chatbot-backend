from urllib.parse import unquote_plus, urlparse

from app import db
from models import AnalyticsEventV2, CatalogoItem, ChatSessionContext, EncEncuesta, EncLink, MarketCart, MarketCartItem, MunicipioTicket, Promocion, PromocionAlcance, PymePedido, TenantFollower, TenantProfile, User
from services.catalog_share import build_catalog_share_payload
from services.catalog_seed import provision_demo_catalog


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
    assert payload["assisted_intake"]["display_name"] == "Vega Marketplace IA"
    assert payload["assisted_intake"]["product_surface"]["name"] == "Vega Marketplace IA"
    assert payload["assisted_intake"]["frontend_contract"]["display_name"] == "Vega Marketplace IA"
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
    assert payload["public_api"]["guest_safe"] is True
    assert payload["public_api"]["catalog"]["endpoint"] == f"/api/market/{tenant.slug}/catalog?contract=marketplace"
    assert payload["public_api"]["catalog"]["alias_endpoint"] == f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace"
    assert payload["public_api"]["cart"]["summary"]["endpoint"] == f"/api/pwa/public/cart/summary?tenant={tenant.slug}"
    assert payload["public_api"]["cart"]["summary"]["guest_safe"] is True
    assert payload["public_api"]["cart"]["add"]["endpoint"] == f"/api/pwa/public/cart/add?tenant={tenant.slug}"
    assert payload["public_api"]["cart"]["add"]["guest_safe"] is True
    assert payload["public_api"]["checkout"]["start"]["endpoint"] == "/api/checkout/crear-preferencia"
    assert payload["public_api"]["checkout"]["fallback_behavior"] == "return_structured_plan_or_payment_error_never_tokenized_endpoint"
    assert payload["public_api"]["assisted_upload"]["endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert payload["public_api"]["security"]["contract_version"] == "marketplace.public_security.v1"
    assert payload["public_api"]["security"]["protected_surfaces"] == ["marketplace_assisted_upload"]
    assert payload["public_api"]["security"]["turnstile"]["contract_version"] == "cloudflare.turnstile.public_intake.v1"
    assert payload["public_api"]["security"]["turnstile"]["provider"] == "cloudflare_turnstile"
    assert payload["public_api"]["security"]["turnstile"]["surface"] == "marketplace_assisted_upload"
    assert payload["public_api"]["security"]["turnstile"]["status"] == "not_required"
    assert payload["public_api"]["security"]["turnstile"]["required"] is False
    assert payload["public_api"]["security"]["turnstile"]["token_header"] == "X-Turnstile-Token"
    assert "turnstile_token" in payload["public_api"]["security"]["turnstile"]["token_fields"]
    assert payload["public_api"]["flow_runtime"]["contract_version"] == "public.whatsapp.flow_runtime.v1"
    assert payload["public_api"]["flow_runtime"]["endpoint"] == f"/api/public/flows/runtime?tenant={tenant.slug}&channel=whatsapp"
    assert payload["public_api"]["flow_runtime"]["actions_endpoint"] == f"/api/public/flows/actions?tenant={tenant.slug}"
    assert payload["public_api"]["flow_runtime"]["guest_safe"] is True
    assert payload["public_api"]["tracking"]["order_path_template"] == f"/tracking/order/{{code}}?tenant_slug={tenant.slug}"
    assert payload["publicCartUrl"].endswith(f"/t/{tenant.slug}/cart")
    assert payload["public_cart_url"].endswith(f"/t/{tenant.slug}/cart")
    assert f"/t/{tenant.slug}/market" in unquote_plus(payload["whatsappShareUrl"])
    assert f"/{tenant.slug}/productos" not in unquote_plus(payload["whatsappShareUrl"])
    assert payload["public_api"]["analytics"]["contract_version"] == "marketplace.public_analytics_loop.v1"
    assert payload["public_api"]["analytics"]["public_client_can_write_events_directly"] is False
    assert payload["public_api"]["analytics"]["event_endpoint"] == "/api/analytics/event"
    assert "catalog_viewed" in payload["public_api"]["analytics"]["recommended_events"]
    assert "checkout_session_created" in payload["public_api"]["analytics"]["recommended_events"]
    assert "order_tracking_opened" in payload["public_api"]["analytics"]["recommended_events"]
    assert payload["frontend_contract"]["show_promotions_strip"] is True
    assert payload["frontend_contract"]["show_faceted_filters"] is True
    assert payload["frontend_contract"]["show_assisted_intake"] is True
    assert payload["frontend_contract"]["public_api_contract"] == "marketplace.public_api.v1"
    assert payload["frontend_contract"]["use_public_api_endpoints"] is True
    assert payload["frontend_contract"]["flow_runtime_endpoint"] == f"/api/public/flows/runtime?tenant={tenant.slug}&channel=whatsapp"
    event = AnalyticsEventV2.query.filter_by(tenant_id=tenant.id, event_name="catalog_viewed").first()
    assert event is not None
    assert event.metadata_payload["contract_version"] == "marketplace.commerce_loop.analytics.v1"
    assert event.metadata_payload["source"] == "public_tenant_catalog_contract"
    assert event.metadata_payload["product_count"] == len(payload["products"])
    assert event.metadata_payload["assisted_intake_mode"] == payload["assisted_intake"]["mode"]


def test_public_market_catalog_preserves_donation_modality_with_points(client):
    tenant = _seed_pyme_tenant_with_catalog()
    donation = CatalogoItem(
        user_id=tenant.pyme_id,
        tenant_id=tenant.id,
        nombre="Bono solidario",
        categoria="Comunidad",
        precio="2500 pts",
        precio_puntos=2500,
        modalidad="donacion",
        disponible=True,
        checkout_type="chatboc",
        sku="bono-solidario",
    )
    db.session.add(donation)
    db.session.commit()

    response = client.get(f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace")

    assert response.status_code == 200
    product = next(item for item in response.get_json()["products"] if item["sku"] == donation.sku)
    assert product["modalidad"] == "donacion"
    assert product["precio_puntos"] == 2500
    assert product["disponible"] is True
    assert product["checkout_type"] == "chatboc"


def test_public_catalog_download_json_exports_marketplace_contract(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get(
        f"/api/public/tenants/{tenant.slug}/catalog/download?format=json",
        headers={"Origin": "https://www.chatboc.ar"},
    )

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
    assert 'filename="catalogo-tienda-demo.json"' in resp.headers.get("Content-Disposition", "")
    body = resp.get_json()
    assert body["contract_version"] == "public.catalog_download.v1"
    assert body["ok"] is True
    assert body["tenant"]["slug"] == tenant.slug
    assert body["catalog"]["contract_version"] == "public.market_catalog.v1"
    assert body["catalog"]["tenant_slug"] == tenant.slug
    assert any(product["nombre"] == "Producto Demo" for product in body["catalog"]["products"])


def test_public_catalog_download_pdf_returns_valid_attachment(client):
    tenant = _seed_pyme_tenant_with_catalog()

    resp = client.get(f"/api/public/tenants/{tenant.slug}/catalog/download?format=pdf")

    assert resp.status_code == 200
    assert "application/pdf" in resp.headers.get("Content-Type", "")
    assert "filename=catalogo-tienda-demo.pdf" in resp.headers.get("Content-Disposition", "")
    assert resp.data.startswith(b"%PDF")
    assert len(resp.data) > 900
    assert resp.headers.get("X-Catalog-Contract-Version") == "public.market_catalog.v1"


def test_catalog_share_payload_download_links_resolve_public_endpoint(client):
    tenant = _seed_pyme_tenant_with_catalog()
    owner = tenant.pyme
    owner.tenant_slug = tenant.slug
    client.application.config["APP_BASE_URL"] = "https://www.chatboc.ar"
    client.application.config["API_BASE_URL"] = "https://api.chatboc.ar"

    with client.application.test_request_context("/"):
        payload = build_catalog_share_payload(owner, channel="whatsapp")

    download_path = urlparse(payload["data"]["catalog_share"]["download_url"]).path
    json_path = urlparse(payload["data"]["catalog_share"]["download_url_json"]).path

    pdf_resp = client.get(f"{download_path}?format=pdf")
    json_resp = client.get(f"{json_path}?format=json")

    assert pdf_resp.status_code == 200
    assert json_resp.status_code == 200
    assert pdf_resp.data.startswith(b"%PDF")
    assert json_resp.get_json()["catalog"]["tenant_slug"] == tenant.slug


def test_public_market_catalog_contract_marks_turnstile_required_when_enforced(client, monkeypatch):
    tenant = _seed_pyme_tenant_with_catalog()
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")
    monkeypatch.setenv("CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-turnstile-secret")
    monkeypatch.setitem(client.application.config, "CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE", "true")
    monkeypatch.setitem(client.application.config, "CLOUDFLARE_TURNSTILE_SECRET_KEY", "test-turnstile-secret")

    resp = client.get(f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace")
    assert resp.status_code == 200
    payload = resp.get_json()

    turnstile = payload["public_api"]["security"]["turnstile"]
    assert turnstile["status"] == "required"
    assert turnstile["configured"] is True
    assert turnstile["enforced"] is True
    assert turnstile["required"] is True
    assert turnstile["reset_required"] is False


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
    assert payload["data_state"] == "catalog_not_configured"
    assert payload["contains_synthetic_demo_data"] is False
    assert payload["synthetic_demo_product_count"] == 0
    assert payload["heroSubtitle"] == "Marketplace asistido para pedidos, boletas y documentos sin registro."
    assert payload["assisted_intake"]["mode"] == "assisted_first"
    assert payload["assisted_intake"]["show_on_empty_catalog"] is True
    assert payload["assisted_intake"]["empty_state"]["primary_cta"] == "Subir pedido o documento"
    assert payload["frontend_contract"]["empty_catalog_mode"] == "assisted_first"


def test_empty_real_municipality_catalog_get_is_side_effect_free(client):
    owner = User(
        email="owner-empty-municipality@test.com",
        name="Municipio vacío",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="municipality-empty-real",
        nombre="Municipio vacío",
        tipo="municipio",
        plan="full",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    for _ in range(2):
        response = client.get(f"/api/public/tenants/{tenant.slug}/catalog")
        assert response.status_code == 200
        assert response.get_json() == []
        assert CatalogoItem.query.filter_by(tenant_id=tenant.id).count() == 0


def test_explicit_demo_catalog_discloses_synthetic_origin(client):
    owner = User(
        email="owner-synthetic-demo@test.com",
        name="Municipio Demo",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="municipality-synthetic-demo",
        nombre="Municipio Demo",
        tipo="municipio",
        plan="full",
        municipio_id=owner.id,
        configuracion={"demo_mode": True, "demo_catalog_seed": True},
    )
    db.session.add(tenant)
    db.session.commit()
    client.application.config["ENABLE_DEMO_MODE"] = True

    assert provision_demo_catalog(owner, tenant) is True
    response = client.get(
        f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data_state"] == "configured"
    assert payload["contains_synthetic_demo_data"] is True
    assert payload["synthetic_demo_product_count"] == len(payload["products"])
    assert payload["products"]
    assert all(product["data_origin"] == "synthetic_demo" for product in payload["products"])
    assert all(product["synthetic_demo"] is True for product in payload["products"])


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
        assert payload["public_api"]["cart"]["summary"]["endpoint"] == f"/api/pwa/public/cart/summary?tenant={tenant.slug}"
        assert payload["public_api"]["cart"]["summary"]["guest_safe"] is True
        assert payload["public_api"]["flow_runtime"]["endpoint"] == f"/api/public/flows/runtime?tenant={tenant.slug}&channel=whatsapp"
    assert set(public_payload.keys()) == set(market_payload.keys())


def test_municipio_market_catalog_contract_exposes_service_request_intake(client):
    tenant = _seed_municipio_tenant_with_catalog()

    resp = client.get(f"/api/market/{tenant.slug}/catalog?contract=marketplace")

    assert resp.status_code == 200
    payload = resp.get_json()
    assisted = payload["assisted_intake"]
    assert "municipio" in assisted["title"].lower()
    public_copy = "\n".join(
        [
            assisted["title"],
            assisted["summary"],
            assisted["empty_state"]["title"],
            assisted["empty_state"]["description"],
        ]
    )
    assert "CRM" not in public_copy
    assert "IA" not in public_copy
    assert any(item["id"] == "service_request" for item in assisted["document_types"])
    assert any(example["document_type"] == "service_request" for example in assisted["text_examples"])
    assert any(item["id"] == "service_request" for item in assisted["use_cases"])
    assert any("boleta" in item["title"].lower() for item in assisted["use_cases"])
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


def test_public_navigation_does_not_expose_draft_only_survey_module(client):
    tenant = _seed_pyme_tenant_with_catalog()
    draft = EncEncuesta(
        tenant_id=tenant.id,
        slug="consulta-interna-no-publicada",
        titulo="Consulta interna",
        descripcion="Todavía no disponible para ciudadanía",
        tipo="opinion",
        estado="borrador",
    )
    db.session.add(draft)
    db.session.commit()

    draft_response = client.get(
        f"/api/public/tenants/{tenant.slug}/public-navigation"
    )
    draft_items = {
        item["id"]: item for item in draft_response.get_json()["items"]
    }
    assert draft_items["surveys"]["enabled"] is False

    draft.estado = "publicada"
    db.session.add(
        EncLink(
            encuesta=draft,
            slug_publico="consulta-ciudadana-publicada",
            canal="web",
        )
    )
    db.session.commit()

    published_response = client.get(
        f"/api/public/tenants/{tenant.slug}/public-navigation"
    )
    published_items = {
        item["id"]: item for item in published_response.get_json()["items"]
    }
    assert published_items["surveys"]["enabled"] is True


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


def test_public_widget_concrete_slug_cannot_be_replaced_by_other_referrer(client):
    tenant_a = _seed_pyme_tenant_with_catalog()
    owner_b = User(email="owner-widget-b@test.com", name="Owner B", rol="admin", tipo_chat="pyme")
    owner_b.set_password("pass")
    db.session.add(owner_b)
    db.session.flush()
    tenant_b = TenantProfile(
        slug="widget-tenant-b",
        nombre="Widget Tenant B",
        tipo="pyme",
        plan="full",
        pyme_id=owner_b.id,
    )
    db.session.add(tenant_b)
    db.session.commit()

    referer = f"https://www.chatboc.ar/t/{tenant_b.slug}"
    cases = (
        ("GET", "/api/public/widget-commerce-session", None),
        ("GET", "/api/public/widget-user/tenant-history", None),
        (
            "POST",
            "/api/public/widget-user/register",
            {"name": "Conflicto", "email": "conflict@test.com"},
        ),
        ("POST", "/api/public/widget-user/link-session", {}),
    )

    for method, path, payload in cases:
        response = client.open(
            path,
            method=method,
            query_string={"tenant_slug": tenant_a.slug},
            json=payload,
            headers={
                "Referer": referer,
                "X-Chat-Session-Id": "chat_widget_conflict",
                "X-Anon-Id": "anon_widget_conflict",
            },
        )
        assert response.status_code == 404, (path, response.get_json())
        assert response.get_json()["reason_code"] == "tenant_resolution_failed"


def test_public_widget_generic_alias_can_use_concrete_referrer(client):
    owner = User(email="owner-widget-ref@test.com", name="Owner Ref", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="widget-ref-tenant",
        nombre="Widget Ref Tenant",
        tipo="pyme",
        plan="full",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    referer = f"https://www.chatboc.ar/t/{tenant.slug}"
    headers = {
        "Referer": referer,
        "X-Chat-Session-Id": "chat_widget_ref_allowed",
        "X-Anon-Id": "anon_widget_ref_allowed",
    }
    query = {"tenant_slug": "municipio", "tenant": "municipio"}

    commerce = client.get(
        "/api/public/widget-commerce-session",
        query_string=query,
        headers=headers,
    )
    history = client.get(
        "/api/public/widget-user/tenant-history",
        query_string=query,
        headers=headers,
    )
    register = client.post(
        "/api/public/widget-user/register",
        query_string=query,
        json={"name": "Cliente Ref", "email": "cliente-ref@test.com"},
        headers=headers,
    )
    link = client.post(
        "/api/public/widget-user/link-session",
        query_string=query,
        json={},
        headers=headers,
    )

    assert commerce.status_code == 200, commerce.get_json()
    assert commerce.get_json()["tenant"]["slug"] == tenant.slug
    assert history.status_code == 200, history.get_json()
    assert history.get_json()["tenant_slug"] == tenant.slug
    assert register.status_code == 200, register.get_json()
    assert register.get_json()["tenant_slug"] == tenant.slug
    assert link.status_code == 200, link.get_json()
    assert link.get_json()["tenant_slug"] == tenant.slug


def test_public_widget_slug_and_widget_token_conflict_fails_closed(client):
    tenant_a = _seed_pyme_tenant_with_catalog()
    owner_b = User(email="owner-widget-token@test.com", name="Owner Token", rol="admin", tipo_chat="pyme")
    owner_b.set_password("pass")
    db.session.add(owner_b)
    db.session.flush()
    tenant_b = TenantProfile(
        slug="widget-token-tenant",
        nombre="Widget Token Tenant",
        tipo="pyme",
        plan="full",
        pyme_id=owner_b.id,
        configuracion={"widget_tokens": ["widget-token-b"]},
    )
    db.session.add(tenant_b)
    db.session.commit()

    response = client.get(
        "/api/public/widget-commerce-session",
        query_string={
            "tenant_slug": tenant_a.slug,
            "widget_token": "widget-token-b",
        },
        headers={
            "X-Chat-Session-Id": "chat_widget_token_conflict",
            "X-Anon-Id": "anon_widget_token_conflict",
        },
    )

    assert response.status_code == 404, response.get_json()
    assert response.get_json()["reason_code"] == "tenant_resolution_failed"


def test_public_widget_rejects_conflicts_hidden_by_selector_precedence(client):
    tenant_a = _seed_pyme_tenant_with_catalog()
    tenant_a.configuracion = {"widget_tokens": ["widget-token-a"]}
    owner_b = User(email="owner-widget-matrix@test.com", name="Owner Matrix", rol="admin", tipo_chat="pyme")
    owner_b.set_password("pass")
    db.session.add(owner_b)
    db.session.flush()
    tenant_b = TenantProfile(
        slug="widget-matrix-b",
        nombre="Widget Matrix B",
        tipo="pyme",
        plan="full",
        pyme_id=owner_b.id,
        configuracion={"widget_tokens": ["widget-token-b"]},
    )
    db.session.add(tenant_b)
    db.session.commit()

    conflicts = (
        (
            {"tenant_slug": tenant_a.slug},
            {"X-Tenant-Slug": tenant_b.slug},
        ),
        (
            {"tenant_slug": tenant_a.slug, "tenant": tenant_b.slug},
            {},
        ),
        (
            {"tenant_slug": tenant_a.slug, "widget_token": "widget-token-a"},
            {"X-Widget-Token": "widget-token-b"},
        ),
    )
    for query, extra_headers in conflicts:
        response = client.get(
            "/api/public/widget-commerce-session",
            query_string=query,
            headers={
                "X-Chat-Session-Id": "chat_widget_matrix",
                "X-Anon-Id": "anon_widget_matrix",
                **extra_headers,
            },
        )
        assert response.status_code == 404, (query, extra_headers, response.get_json())
        assert response.get_json()["reason_code"] == "tenant_resolution_failed"

    consistent = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": tenant_a.slug, "tenant": tenant_a.slug},
        headers={
            "X-Tenant-Slug": tenant_a.slug,
            "X-Chat-Session-Id": "chat_widget_matrix_same",
            "X-Anon-Id": "anon_widget_matrix_same",
        },
    )
    assert consistent.status_code == 200, consistent.get_json()
    assert consistent.get_json()["tenant"]["slug"] == tenant_a.slug


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
    assert body["catalog"]["marketplace_endpoint"] == f"/api/public/tenants/{tenant.slug}/catalog?contract=marketplace"
    assert body["catalog"]["products_count"] == 2
    assert body["catalog"]["has_products"] is True
    assert body["catalog"]["assisted_intake_enabled"] is True
    assert body["catalog"]["assisted_upload_endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert body["assisted_intake"]["contract_version"] == "marketplace.assisted_intake_entry.v1"
    assert body["assisted_intake"]["mode"] == "catalog_plus_assisted"
    assert body["assisted_intake"]["anonymous_intake"] is True
    assert body["assisted_intake"]["submit"]["endpoint"] == "/api/pedidos/from-file?origen=marketplace"
    assert "marketplace.upload_order_from_file.v1" in body["assisted_intake"]["submit"]["aliases"]
    assert body["assisted_intake"]["submit"]["component_contract"] == "marketplace.upload_order_from_file.v1"
    assert body["assisted_intake"]["frontend_contract"]["supports_anonymous_follow_up"] is True
    assert body["cart"]["summary_endpoint"] == "/api/pwa/public/cart/summary"
    assert body["cart"]["items_endpoint"] == "/api/pwa/public/cart/items"
    assert body["cart"]["legacy_endpoint"] == "/api/pwa/public/cart"
    assert body["cart"]["public_api"]["summary"]["endpoint"] == f"/api/pwa/public/cart/summary?tenant={tenant.slug}"
    assert body["cart"]["public_api"]["add"]["endpoint"] == f"/api/pwa/public/cart/add?tenant={tenant.slug}"
    assert body["public_api"]["contract_version"] == "marketplace.public_api.v1"
    assert body["public_api"]["guest_safe"] is True
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
    assert "assisted_upload" in body["frontend_contract"]["primary_actions"]
    assert body["frontend_contract"]["supports_anonymous_assisted_upload"] is True
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
    assert resp.headers.get("X-Request-Id")


def test_widget_commerce_session_empty_municipio_promotes_assisted_upload(client):
    owner = User(email="empty-muni@test.com", name="Municipio sin catalogo", rol="admin", tipo_chat="municipio")
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="junin-empty",
        nombre="Municipalidad de Junin",
        tipo="municipio",
        plan="full",
        municipio_id=owner.id,
        vertical="gobierno",
    )
    db.session.add(tenant)
    db.session.commit()

    resp = client.get(
        "/api/public/widget-commerce-session",
        query_string={"tenant_slug": tenant.slug},
        headers={
            "Origin": "https://www.chatboc.ar",
            "X-Chat-Session-Id": "chat_empty_muni",
            "X-Anon-Id": "anon_empty_muni",
        },
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tenant"]["slug"] == tenant.slug
    assert body["catalog"]["enabled"] is True
    assert body["catalog"]["products_count"] == 0
    assert body["catalog"]["has_products"] is False
    assert body["catalog"]["empty_catalog_mode"] == "assisted_first"
    assert body["assisted_intake"]["mode"] == "assisted_first"
    assert body["assisted_intake"]["show_on_empty_catalog"] is True
    assert body["assisted_intake"]["empty_state"]["primary_cta"] == "Subir pedido o documento"
    assert "service_request" in {item["id"] for item in body["assisted_intake"]["document_types"]}
    assert body["assisted_intake"]["frontend_contract"]["render_as"] == "marketplace_assisted_intake"
    assert body["frontend_contract"]["empty_state_behavior"] == "assisted_intake_first"
    assert body["frontend_contract"]["primary_actions"][1] == "assisted_upload"


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

    viewer = User(
        email="viewer-history@test.com",
        name="Viewer History",
        rol="usuario",
        anon_id="anon_public_2",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    viewer.set_password("pass")
    db.session.add(viewer)
    db.session.flush()
    db.session.add(
        ChatSessionContext(
            chat_session_id="chat_public_2",
            tenant_id=tenant.id,
            user_id=viewer.id,
        )
    )

    cart = MarketCart(
        tenant_id=tenant.id,
        user_id=viewer.id,
        session_id="chat_public_2",
        status="open",
    )
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
        user_id=viewer.id,
        anon_id="anon_public_2",
        canal_ingreso="widget",
    )
    pedido = PymePedido(
        pyme_id=owner.id,
        asunto="Pedido web",
        detalles="{}",
        monto_total=100.0,
        tenant_id=tenant.id,
        user_id=viewer.id,
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
