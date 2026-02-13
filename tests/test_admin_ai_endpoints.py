from datetime import datetime, timedelta

from extensions import db
from models import CatalogoItem, MunicipioTicket, PymePedido, PymeTicket, TicketComentario, User


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


def test_executive_summary_tenant_scoped(client, monkeypatch):
    monkeypatch.setattr(
        "routes.admin_ai.generate_analytics_report",
        lambda stats, tenant_type="pyme": {"summary": f"ok-{tenant_type}", "opportunities": [], "threats": []},
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

    forbidden = client.post(
        "/admin/ai/executive-summary",
        json={"tenant_id": tenant_id, "scope": "municipio"},
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": "999"},
    )
    assert forbidden.status_code == 403


def test_ticket_ai_summary_tenant_scoped(client, monkeypatch):
    monkeypatch.setattr(
        "routes.admin_ai.generate_ticket_summary",
        lambda ticket: {"summary": f"ticket-{ticket['id']}", "next_steps": [], "confidence": "high"},
    )

    tenant_id = 42
    admin = _ensure_user(tenant_id, "pyme")
    ticket = PymeTicket(
        user_id=tenant_id,
        tenant_id=tenant_id,
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
        headers={"X-Debug-Role": "operador", "X-Debug-Tenant": str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ai"]["summary"] == f"ticket-{ticket.id}"

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
