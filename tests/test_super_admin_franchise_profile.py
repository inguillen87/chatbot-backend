import jwt

from app import db
from models import TenantProfile, User


def _sa_headers(app, super_admin_user):
    token = jwt.encode(
        {
            "user_id": super_admin_user.id,
            "rol": super_admin_user.rol,
            "tipo_chat": super_admin_user.tipo_chat,
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_super_admin_franchise_profile_get_put(client, app):
    sa = User(email="sa@test.com", name="SA", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner@test.com", name="Owner", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()

    tenant = TenantProfile(slug="tenant-fr", nombre="Tenant FR", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    headers = _sa_headers(app, sa)

    get_resp = client.get("/api/admin/tenants/tenant-fr/franchise-profile", headers=headers)
    assert get_resp.status_code == 200
    data = get_resp.get_json()
    assert data["tenant"]["slug"] == "tenant-fr"
    assert {"es", "en", "pt"}.issubset(set(data["franchise_profile"]["supported_languages"]))

    put_resp = client.put(
        "/api/admin/tenants/tenant-fr/franchise-profile",
        headers=headers,
        json={
            "default_language": "en",
            "supported_languages": ["en", "pt"],
            "currency": "USD",
            "country": "US",
            "partner_program": "enterprise_reseller",
            "target_markets": ["north_america", "latam"],
        },
    )
    assert put_resp.status_code == 200
    profile = put_resp.get_json()["franchise_profile"]
    assert profile["default_language"] == "en"
    assert profile["currency"] == "USD"
    assert "pt" in profile["supported_languages"]

    tenant_refreshed = TenantProfile.query.filter_by(slug="tenant-fr").first()
    assert tenant_refreshed.configuracion["franchise_profile"]["country"] == "US"


def test_super_admin_franchise_profile_rejects_invalid_language(client, app):
    sa = User(email="sa2@test.com", name="SA2", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner2@test.com", name="Owner2", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()

    tenant = TenantProfile(slug="tenant-fr-2", nombre="Tenant FR 2", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()

    headers = _sa_headers(app, sa)
    resp = client.put(
        "/api/admin/tenants/tenant-fr-2/franchise-profile",
        headers=headers,
        json={"default_language": "de"},
    )
    assert resp.status_code == 400


def test_super_admin_franchise_readiness(client, app):
    sa = User(email="sa3@test.com", name="SA3", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner3@test.com", name="Owner3", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()

    tenant = TenantProfile(
        slug="tenant-fr-3",
        nombre="Tenant FR 3",
        tipo="pyme",
        pyme_id=owner.id,
        dominio="tenant-fr-3.example.com",
        logo_url="https://cdn.example.com/logo.png",
        whatsapp_sender_id="5491112345678",
        configuracion={
            "mercadopago_access_token": "TEST-123",
            "franchise_profile": {
                "white_label_enabled": True,
                "reseller_enabled": True,
                "target_markets": ["latam", "na"],
                "default_language": "en",
                "supported_languages": ["en", "es", "pt"],
                "timezone": "America/New_York",
                "currency": "USD",
                "country": "US",
                "legal_entity_name": "Tenant FR 3 LLC",
                "partner_program": "global_partner",
            },
        },
    )
    db.session.add(tenant)
    db.session.commit()

    headers = _sa_headers(app, sa)
    resp = client.get("/api/admin/tenants/tenant-fr-3/franchise-readiness", headers=headers)
    assert resp.status_code == 200
    payload = resp.get_json()
    readiness = payload["readiness"]
    assert readiness["status"] in {"ready", "in_progress", "basic"}
    assert readiness["score"] >= 85
    assert readiness["checks"]["payments_configured"] is True
