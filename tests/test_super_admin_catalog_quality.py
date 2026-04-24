import jwt

from app import db
from models import CatalogoItem, TenantProfile, User


def _sa_headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_catalog_quality_queue(client, app):
    sa = User(email="sa-quality@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    owner = User(email="owner-quality@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    sa.set_password("pass")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()

    tenant = TenantProfile(slug="quality-tenant", nombre="Quality Tenant", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    flagged = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto Dudoso",
        categoria="General",
        precio="0",
        extra_metadata={"confidence_score": 0.41, "quality_issues": ["precio_invalido"], "review_required": True},
    )
    clean = CatalogoItem(
        user_id=owner.id,
        tenant_id=tenant.id,
        nombre="Producto OK",
        categoria="General",
        precio="1500",
        extra_metadata={"confidence_score": 0.95, "quality_issues": [], "review_required": False},
    )
    db.session.add_all([flagged, clean])
    db.session.commit()

    resp = client.get("/api/admin/catalog/quality?tenant_slug=quality-tenant", headers=_sa_headers(app, sa))
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["total"] == 1
    assert payload["items"][0]["nombre"] == "Producto Dudoso"
    assert payload["items"][0]["review_required"] is True
