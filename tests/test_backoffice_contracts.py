import jwt
from datetime import datetime, timedelta

from flask import current_app

from extensions import db
from models import EncComentario, EncEncuesta, EncRespuesta, MunicipioTicket, TenantProfile, User


def _auth_headers(user: User) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)},
        current_app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return {"Authorization": f"Bearer {token}"}


def test_backoffice_navigation_exposes_role_based_modules(client):
    owner = User(
        email="backoffice-junin@test.com",
        name="Backoffice Junin",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="junin-backoffice",
        municipio_id=601,
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="junin-backoffice",
        nombre="Junin Backoffice",
        tipo="municipio",
        municipio_id=owner.id,
        plan="enterprise",
        capabilities_json={"advanced_analytics": True, "surveys": True, "maps": True, "people": True},
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers={**_auth_headers(owner), "X-Request-Id": "req-backoffice-nav-1"},
    )

    assert response.status_code == 200
    assert response.headers.get("X-Request-Id") == "req-backoffice-nav-1"
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.navigation.v1"
    assert payload["tenant_slug"] == tenant.slug
    assert payload["role"] == "admin"
    module_ids = [item["id"] for item in payload["modules"] if item["enabled"]]
    assert {"operations", "reports", "surveys", "people", "maps", "advanced_analytics"}.issubset(set(module_ids))
    assert payload["analytics_modes"]["statistics"]["enabled"] is True
    assert payload["analytics_modes"]["advanced_analytics"]["enabled"] is True
    assert payload["surveys_overview"]["route"] == "/admin/encuestas"


def test_backoffice_summary_counts_real_operations_and_surveys(client):
    owner = User(
        email="backoffice-summary@test.com",
        name="Backoffice Summary",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="summary-tenant",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="summary-tenant",
        nombre="Summary Tenant",
        tipo="municipio",
        municipio_id=owner.id,
        plan="pro",
        capabilities_json={"advanced_analytics": True, "surveys": True},
    )
    db.session.add(tenant)
    db.session.flush()

    now = datetime.utcnow()
    db.session.add(
        MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            pregunta="Luminaria apagada",
            categoria="luminarias",
            estado="nuevo",
            fecha=now - timedelta(days=1),
            latitud=-34.6,
            longitud=-58.4,
        )
    )
    db.session.add(
        MunicipioTicket(
            municipio_id=owner.id,
            tenant_id=tenant.id,
            pregunta="Caso resuelto",
            categoria="limpieza",
            estado="resuelto",
            fecha=now - timedelta(days=2),
        )
    )
    encuesta = EncEncuesta(
        tenant_id=tenant.id,
        slug="summary-survey",
        titulo="Sondeo Summary",
        estado="publicada",
        permitir_comentarios=True,
    )
    db.session.add(encuesta)
    db.session.flush()
    db.session.add(
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=tenant.id,
            huella_unica="summary-fp",
            lat=-34.61,
            lng=-58.41,
            submitted_at=now - timedelta(hours=3),
        )
    )
    db.session.add(
        EncComentario(
            encuesta_id=encuesta.id,
            texto="Revisar comentario",
            estado="revision",
        )
    )
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/summary",
        query_string={"tenant_slug": tenant.slug, "window": "7d"},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "backoffice.summary.v1"
    assert payload["title"] == "Resumen de los ultimos 7d"
    cards = {item["id"]: item for item in payload["cards"]}
    assert cards["pending_cases"]["value"] == 1
    assert cards["resolved_cases"]["value"] == 1
    assert cards["active_surveys"]["value"] == 1
    assert cards["live_votes"]["value"] == 1
    assert payload["surveys_overview"]["comments_pending_review"] == 1
    assert payload["surveys_overview"]["heatmap_available"] is True
    assert payload["ai_summary_available"] is True
    assert any(item["id"] == "top_pending_category" for item in payload["priorities"])


def test_backoffice_navigation_disables_unavailable_enterprise_modules(client):
    owner = User(
        email="backoffice-free@test.com",
        name="Backoffice Free",
        rol="admin",
        tipo_chat="pyme",
        tenant_slug="free-pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="free-pyme",
        nombre="Free Pyme",
        tipo="pyme",
        pyme_id=owner.id,
        plan="free",
        capabilities_json={"advanced_analytics": False, "surveys": False, "maps": False},
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.get(
        "/api/app/backoffice/navigation",
        query_string={"tenant_slug": tenant.slug},
        headers=_auth_headers(owner),
    )

    assert response.status_code == 200
    modules = {item["id"]: item for item in response.get_json()["modules"]}
    assert modules["surveys"]["enabled"] is False
    assert modules["maps"]["enabled"] is False
    assert modules["advanced_analytics"]["enabled"] is False
