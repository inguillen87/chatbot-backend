from app import db
from models import TenantProfile, User
from tests.auth_test_utils import clerk_superadmin_headers


def test_directory_read_never_overwrites_an_existing_seeded_organization(client, app):
    sa = User(email="sa-seed@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    owner = User(email="mauricio@junin.com", name="Owner", rol="admin_municipio", tipo_chat="municipio")
    sa.set_password("pass")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()
    tenant = TenantProfile(slug="junin", nombre="Municipalidad de Junín", tipo="municipio",
                           plan="pro", municipio_id=owner.id)
    db.session.add(tenant)
    db.session.commit()
    owner.tenant_id = tenant.id
    db.session.commit()

    for _ in range(2):
        resp = client.get("/api/admin/tenants", headers=clerk_superadmin_headers(sa))
        assert resp.status_code == 200

    db.session.expire_all()
    stored = db.session.get(TenantProfile, tenant.id)
    assert stored.nombre == "Municipalidad de Junín"
    assert stored.plan == "pro"
    assert stored.slug == "junin"
