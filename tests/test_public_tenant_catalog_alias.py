from app import db
from models import CatalogoItem, TenantProfile, User


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
