import io
import jwt
from datetime import datetime, timedelta

from flask import current_app

from extensions import db
from models import CatalogoItem, MunicipioTicket, PymePedido, PymeTicket, TenantProfile, TicketComentario, User


def _ensure_user(tenant_id: int, scope: str) -> User:
    user = User.query.get(tenant_id)
    if user:
        return user
    user = User(
        id=tenant_id,
        name=f"Tenant {tenant_id}",
        email=f"tenant_{scope}_{tenant_id}@example.com",
        rol="admin",
        tipo_chat=scope,
        municipio_id=tenant_id if scope == "municipio" else None,
        pyme_id=tenant_id if scope == "pyme" else None,
    )
    user.set_password("test")
    db.session.add(user)
    db.session.flush()
    return user


def _create_tenant(owner_id: int, scope: str, slug: str) -> tuple[User, TenantProfile]:
    owner = _ensure_user(owner_id, scope)
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo=scope,
        municipio_id=owner.id if scope == "municipio" else None,
        pyme_id=owner.id if scope == "pyme" else None,
    )
    db.session.add(tenant)
    db.session.flush()
    return owner, tenant


def _auth_headers(user: User) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)},
        current_app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return {"Authorization": f"Bearer {token}"}


def test_executive_summary_tenant_scoped(client, monkeypatch):
    monkeypatch.setattr(
        "routes.admin_ai.generate_backoffice_analytics_summary",
        lambda stats, tenant_type="pyme": {
            "summary": f"ok-{tenant_type}",
            "opportunities": [],
            "threats": [],
            "meta": {"task_type": "analytics", "secret_values_exposed": False},
        },
    )

    tenant_id = 41
    _ensure_user(tenant_id, "municipio")
    db.session.add(
        MunicipioTicket(
            municipio_id=tenant_id,
            tenant_id=tenant_id,
            pregunta="Luminaria caída",
            categoria="alumbrado",
            fecha=datetime.utcnow() - timedelta(hours=2),
        )
    )
    db.session.commit()

    response = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": tenant_id, "scope": "municipio", "from": "2024-01-01", "to": "2026-01-01"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ai"]["summary"] == "ok-municipio"
    assert payload["ai"]["meta"]["task_type"] == "analytics"
    assert payload["ai"]["meta"]["secret_values_exposed"] is False

    forbidden = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": tenant_id, "scope": "municipio"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert forbidden.status_code == 403


def test_ticket_ai_summary_tenant_scoped(client, monkeypatch):
    monkeypatch.setattr(
        "routes.admin_ai.generate_backoffice_ticket_summary",
        lambda ticket: {
            "summary": f"ticket-{ticket['id']}",
            "next_steps": [],
            "confidence": "high",
            "meta": {"task_type": "ticket_summary", "secret_values_exposed": False},
        },
    )

    owner_id = 42
    admin, tenant = _create_tenant(owner_id, "pyme", "ticket-summary-pyme")
    ticket = PymeTicket(
        user_id=admin.id,
        tenant_id=tenant.id,
        pregunta="No llegó el pedido",
        categoria="delivery",
        estado="en_progreso",
        nro_ticket=9876543,
        fecha=datetime.utcnow() - timedelta(days=1),
    )
    db.session.add(ticket)
    db.session.flush()
    db.session.add(
        TicketComentario(
            pyme_ticket_id=ticket.id,
            comentario="Estamos verificando con logística",
            fecha=datetime.utcnow(),
            es_admin=True,
            estado_ticket="en_progreso",
            user_id=admin.id,
        )
    )
    db.session.commit()

    response = client.post(
        f"/admin/tickets/{ticket.id}/ai-summary",
        json={"scope": "pyme"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ai"]["summary"] == f"ticket-{ticket.id}"
    assert payload["ai"]["meta"]["task_type"] == "ticket_summary"
    assert payload["ai"]["meta"]["secret_values_exposed"] is False

    forbidden = client.post(
        f"/admin/tickets/{ticket.id}/ai-summary",
        json={"scope": "pyme"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert forbidden.status_code == 403


def test_product_recommendations_tenant_scoped(client):
    tenant_id = 43
    owner = _ensure_user(tenant_id, "pyme")

    item_fast = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant_id,
        nombre="Malbec Reserva",
        categoria="vinos",
        modalidad="venta",
        precio="10000",
        disponible=True,
    )
    item_other = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant_id,
        nombre="Cabernet",
        categoria="vinos",
        modalidad="canje",
        precio_puntos=500,
        disponible=True,
    )
    db.session.add_all([item_fast, item_other])
    db.session.flush()

    db.session.add(
        PymePedido(
            pyme_id=owner.id,
            tenant_id=tenant_id,
            asunto="Pedido pruebas",
            detalles='[{"nombre": "Malbec Reserva", "qty": 2}]',
            monto_total=20000,
        )
    )
    db.session.commit()

    response = client.post(
        "/admin/ai/product-recommendations",
        json={"tenant_id": tenant_id, "limit": 2},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["recommendations"]
    assert payload["recommendations"][0]["nombre"] == "Malbec Reserva"

    forbidden = client.post(
        "/admin/ai/product-recommendations",
        json={"tenant_id": tenant_id},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert forbidden.status_code == 403


def test_order_draft_from_document_matches_catalog(client, monkeypatch):
    tenant_id = 44
    owner = _ensure_user(tenant_id, "pyme")

    item = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant_id,
        nombre="Taladro Pro",
        categoria="herramientas",
        modalidad="venta",
        precio="150000",
        disponible=True,
    )
    db.session.add(item)
    db.session.commit()

    monkeypatch.setattr(
        "routes.admin_ai.analyze_text_structured",
        lambda text, prompt: {"items": [{"nombre": "Taladro Pro", "cantidad": 2}]},
    )
    monkeypatch.setattr("routes.admin_ai.analyze_image_text", lambda content: "Taladro Pro x2")

    response = client.post(
        "/admin/ai/order-draft-from-document",
        data={
            "tenant_id": str(tenant_id),
            "file": (io.BytesIO(b"fake-image"), "nota.png"),
        },
        content_type="multipart/form-data",
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["draft_items"]
    assert payload["draft_items"][0]["match_status"] == "matched"
    assert payload["draft_items"][0]["catalogo_item_id"] == item.id
    assert payload["matched_count"] == 1
    assert payload["unmatched_count"] == 0

    forbidden = client.post(
        "/admin/ai/order-draft-from-document",
        data={
            "tenant_id": str(tenant_id),
            "file": (io.BytesIO(b"fake-image"), "nota.png"),
        },
        content_type="multipart/form-data",
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert forbidden.status_code == 403


def test_order_draft_rejects_unsupported_file_type(client):
    tenant_id = 45
    _ensure_user(tenant_id, "pyme")

    response = client.post(
        "/admin/ai/order-draft-from-document",
        data={
            "tenant_id": str(tenant_id),
            "file": (io.BytesIO(b"hello"), "nota.txt"),
        },
        content_type="multipart/form-data",
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 400


def test_executive_summary_reports_insufficient_data_without_ai_call(client, monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("AI should not be called for empty periods")

    monkeypatch.setattr("routes.admin_ai.generate_backoffice_analytics_summary", _boom)

    tenant_id = 46
    _ensure_user(tenant_id, "municipio")
    db.session.commit()

    response = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": tenant_id, "scope": "municipio", "from": "2024-01-01", "to": "2024-01-02", "strict_no_data_message": True},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ai"]["tone"] == "Data-Insufficient"


def test_ticket_ai_summary_falls_back_without_provider(client, monkeypatch):
    def _boom(ticket):
        raise RuntimeError("provider down")

    monkeypatch.setattr("services.ai_backoffice_summaries.llamar_llm_con_fallback", _boom)

    owner_id = 49
    owner, tenant = _create_tenant(
        owner_id,
        "municipio",
        "ticket-summary-fallback-municipio",
    )
    ticket = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Luminaria rota en plaza",
        categoria="alumbrado",
        estado="nuevo",
        nro_ticket=123,
        fecha=datetime.utcnow() - timedelta(hours=1),
    )
    db.session.add(ticket)
    db.session.commit()

    response = client.post(
        f"/admin/tickets/{ticket.id}/ai-summary",
        json={"scope": "municipio"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ai"]["meta"]["fallback"] is True
    assert payload["ai"]["meta"]["provider"] == "deterministic_local_fallback"
    assert payload["ai"]["meta"]["policy"]["task_type"] == "ticket_summary"


def test_bot_settings_get_and_put_tenant_scoped(client):
    tenant_id = 47
    owner = _ensure_user(tenant_id, "pyme")
    tenant = TenantProfile(
        slug="tenant-bot-settings",
        nombre="Tenant Bot Settings",
        tipo="pyme",
        pyme_id=owner.id,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.commit()

    get_response = client.get(
        "/admin/bot/settings",
        query_string={"tenant_id": tenant.id},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )
    assert get_response.status_code == 200
    initial_payload = get_response.get_json()
    assert initial_payload["settings"]["name"] is None

    update_response = client.put(
        "/admin/bot/settings",
        json={
            "tenant_id": tenant.id,
            "name": "Asistente Bodega",
            "tone": "profesional",
            "system_prompt": "Ayuda con pedidos y postventa.",
            "fallback_behavior": "derivar_humano",
            "branding": {
                "logo_url": "https://example.com/logo.png",
                "primary_color": "#123456",
                "secondary_color": "#654321",
            },
        },
        headers={"X-Debug-Role": "admin", "X-Debug-Tenant": str(tenant.id)},
    )
    assert update_response.status_code == 200
    updated_payload = update_response.get_json()
    assert updated_payload["settings"]["name"] == "Asistente Bodega"
    assert updated_payload["settings"]["fallback_behavior"] == "derivar_humano"
    assert updated_payload["settings"]["branding"]["logo_url"] == "https://example.com/logo.png"

    refreshed_tenant = TenantProfile.query.get(tenant.id)
    assert refreshed_tenant.logo_url == "https://example.com/logo.png"
    assert refreshed_tenant.configuracion["bot_settings"]["tone"] == "profesional"


def test_bot_settings_forbidden_cross_tenant(client):
    tenant_id = 48
    owner = _ensure_user(tenant_id, "pyme")
    tenant = TenantProfile(
        slug="tenant-bot-forbidden",
        nombre="Tenant Forbidden",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/admin/bot/settings",
        query_string={"tenant_id": tenant.id},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert response.status_code == 403


def test_bot_settings_rejects_invalid_payload(client):
    tenant_id = 49
    owner = _ensure_user(tenant_id, "pyme")
    tenant = TenantProfile(
        slug="tenant-bot-invalid",
        nombre="Tenant Invalid",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.put(
        "/admin/bot/settings",
        json={
            "tenant_id": tenant.id,
            "fallback_behavior": "invalid_behavior",
        },
        headers={"X-Debug-Role": "admin", "X-Debug-Tenant": str(tenant.id)},
    )
    assert response.status_code == 400


def test_bot_settings_api_admin_alias(client):
    tenant_id = 50
    owner = _ensure_user(tenant_id, "pyme")
    tenant = TenantProfile(
        slug="tenant-bot-alias",
        nombre="Tenant Alias",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    get_response = client.get(
        "/api/admin/bot/settings",
        query_string={"tenant_id": tenant.id},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant.id)},
    )
    assert get_response.status_code == 200

    put_response = client.put(
        "/api/admin/bot/settings",
        json={"tenant_id": tenant.id, "name": "Alias Bot"},
        headers={"X-Debug-Role": "admin", "X-Debug-Tenant": str(tenant.id)},
    )
    assert put_response.status_code == 200
    payload = put_response.get_json()
    assert payload["settings"]["name"] == "Alias Bot"


def test_ticket_ai_summary_employee_category_scope_blocks_provider_for_restricted_ticket(client, monkeypatch):
    owner, tenant = _create_tenant(801, "municipio", "ai-summary-employee-scope")
    employee = User(
        email="ai-summary-employee@test.com",
        name="AI Summary Employee",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        empresa_id=owner.id,
        ticket_categorias="alumbrado",
    )
    employee.set_password("pw")
    end_user = User(
        email="ai-summary-end-user@test.com",
        name="AI Summary End User",
        rol="usuario",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    )
    end_user.set_password("pw")
    allowed = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Luminaria sin luz",
        categoria="alumbrado",
        estado="nuevo",
    )
    restricted = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Bache profundo",
        categoria="baches",
        estado="nuevo",
    )
    db.session.add_all([employee, end_user, allowed, restricted])
    db.session.commit()

    provider_calls = []

    def fake_summary(ticket_payload):
        provider_calls.append(ticket_payload)
        return {"summary": "ok", "next_steps": [], "confidence": "high"}

    monkeypatch.setattr("routes.admin_ai.generate_backoffice_ticket_summary", fake_summary)

    allowed_response = client.post(
        f"/admin/tickets/{allowed.id}/ai-summary",
        json={"scope": "municipio"},
        headers=_auth_headers(employee),
    )
    assert allowed_response.status_code == 200
    assert [call["id"] for call in provider_calls] == [allowed.id]

    provider_calls.clear()
    restricted_response = client.post(
        f"/admin/tickets/{restricted.id}/ai-summary",
        json={"scope": "municipio"},
        headers=_auth_headers(employee),
    )
    assert restricted_response.status_code == 404
    assert provider_calls == []

    end_user_response = client.post(
        f"/admin/tickets/{allowed.id}/ai-summary",
        json={"scope": "municipio"},
        headers=_auth_headers(end_user),
    )
    assert end_user_response.status_code == 403
    assert provider_calls == []


def test_ticket_ai_enrichment_employee_category_scope_blocks_builder_for_restricted_ticket(client, monkeypatch):
    owner, tenant = _create_tenant(802, "municipio", "ai-enrichment-employee-scope")
    employee = User(
        email="ai-enrichment-employee@test.com",
        name="AI Enrichment Employee",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        empresa_id=owner.id,
        ticket_categorias="alumbrado",
    )
    employee.set_password("pw")
    allowed = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Poste sin luz",
        categoria="alumbrado",
        estado="nuevo",
    )
    restricted = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        pregunta="Residuo sin retirar",
        categoria="limpieza",
        estado="nuevo",
    )
    db.session.add_all([employee, allowed, restricted])
    db.session.commit()

    enrichment_calls = []

    def fake_enrichment(ticket, **kwargs):
        enrichment_calls.append((ticket.id, kwargs))
        return {"ticket_id": ticket.id, "suggested_category": ticket.categoria}

    monkeypatch.setattr("services.ticket_ai_enrichment.build_ticket_ai_enrichment", fake_enrichment)

    allowed_response = client.post(
        f"/admin/tickets/{allowed.id}/ai-enrichment",
        json={"scope": "municipio"},
        headers=_auth_headers(employee),
    )
    assert allowed_response.status_code == 200
    assert [call[0] for call in enrichment_calls] == [allowed.id]

    enrichment_calls.clear()
    restricted_response = client.post(
        f"/admin/tickets/{restricted.id}/ai-enrichment",
        json={"scope": "municipio"},
        headers=_auth_headers(employee),
    )
    assert restricted_response.status_code == 404
    assert enrichment_calls == []


def test_executive_ai_intersects_employee_category_scope_before_aggregation_and_provider(client, monkeypatch):
    owner, tenant = _create_tenant(803, "municipio", "ai-executive-employee-scope")
    employee = User(
        email="ai-executive-employee@test.com",
        name="AI Executive Employee",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
        empresa_id=owner.id,
        ticket_categorias="alumbrado",
    )
    employee.set_password("pw")
    db.session.add(employee)
    db.session.commit()

    captured_filters = []
    provider_metrics = []

    def fake_get_summary(filters):
        captured_filters.append(filters)
        ticket_count = 1 if filters.categorias == ("alumbrado",) else 0
        return {"totals": {"tickets": ticket_count, "pedidos": 0}}

    def fake_provider(metrics, tenant_type="municipio"):
        provider_metrics.append(metrics)
        return {"summary": "scoped", "opportunities": [], "threats": []}

    monkeypatch.setattr("routes.admin_ai.get_summary", fake_get_summary)
    monkeypatch.setattr("routes.admin_ai.generate_backoffice_analytics_summary", fake_provider)

    allowed_response = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": owner.id, "scope": "municipio"},
        headers=_auth_headers(employee),
    )
    assert allowed_response.status_code == 200
    assert captured_filters[-1].categorias == ("alumbrado",)
    assert provider_metrics[-1]["totals"]["tickets"] == 1

    restricted_response = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": owner.id, "scope": "municipio", "categoria": "baches"},
        headers=_auth_headers(employee),
    )
    assert restricted_response.status_code == 200
    assert captured_filters[-1].categorias == ("__no_authorized_employee_category__",)
    assert provider_metrics[-1]["totals"]["tickets"] == 0
