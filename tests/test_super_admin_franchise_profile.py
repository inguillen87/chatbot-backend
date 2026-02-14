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


def test_super_admin_franchise_playbook(client, app):
    sa = User(email="sa4@test.com", name="SA4", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner = User(email="owner4@test.com", name="Owner4", rol="admin", tipo_chat="pyme")
    owner.set_password("pass")
    db.session.add_all([sa, owner])
    db.session.commit()

    tenant = TenantProfile(
        slug="tenant-fr-4",
        nombre="Tenant FR 4",
        tipo="pyme",
        pyme_id=owner.id,
        configuracion={
            "franchise_profile": {
                "white_label_enabled": False,
                "reseller_enabled": False,
                "default_language": "es",
                "supported_languages": ["es"],
                "currency": "",
                "country": "",
                "timezone": "",
                "partner_program": "",
            },
        },
    )
    db.session.add(tenant)
    db.session.commit()

    headers = _sa_headers(app, sa)
    resp = client.get('/api/admin/tenants/tenant-fr-4/franchise-playbook', headers=headers)
    assert resp.status_code == 200
    payload = resp.get_json()
    playbook = payload['playbook']
    assert isinstance(playbook['next_actions'], list)
    assert len(playbook['next_actions']) >= 3
    assert playbook['next_actions'][0]['priority'] in {'high', 'medium', 'low'}
    assert 'phase_1' in playbook['estimated_phases']


def test_super_admin_franchise_compare_ranking_and_filters(client, app):
    sa = User(email="sa5@test.com", name="SA5", rol="super_admin", tipo_chat="admin")
    sa.set_password("pass")
    owner1 = User(email="owner5a@test.com", name="Owner5A", rol="admin", tipo_chat="pyme")
    owner1.set_password("pass")
    owner2 = User(email="owner5b@test.com", name="Owner5B", rol="admin", tipo_chat="pyme")
    owner2.set_password("pass")
    db.session.add_all([sa, owner1, owner2])
    db.session.commit()

    tenant_ready = TenantProfile(
        slug="tenant-fr-ready",
        nombre="Tenant Ready",
        tipo="pyme",
        pyme_id=owner1.id,
        dominio="ready.example.com",
        logo_url="https://cdn.example.com/ready.png",
        whatsapp_sender_id="5491111111111",
        configuracion={
            "mercadopago_access_token": "TOKEN-READY",
            "franchise_profile": {
                "white_label_enabled": True,
                "reseller_enabled": True,
                "target_markets": ["latam", "na"],
                "default_language": "en",
                "supported_languages": ["en", "es", "pt"],
                "timezone": "America/New_York",
                "currency": "USD",
                "country": "US",
                "legal_entity_name": "Tenant Ready LLC",
                "partner_program": "global_partner",
            },
        },
    )

    tenant_basic = TenantProfile(
        slug="tenant-fr-basic",
        nombre="Tenant Basic",
        tipo="pyme",
        pyme_id=owner2.id,
        configuracion={
            "franchise_profile": {
                "white_label_enabled": False,
                "reseller_enabled": False,
                "default_language": "es",
                "supported_languages": ["es"],
                "currency": "",
                "country": "",
                "timezone": "",
                "partner_program": "",
            },
        },
    )

    db.session.add_all([tenant_ready, tenant_basic])
    db.session.commit()

    headers = _sa_headers(app, sa)

    resp = client.get('/api/admin/tenants/franchise-compare', headers=headers)
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload['total'] >= 2
    assert payload['items'][0]['tenant']['slug'] == 'tenant-fr-ready'
    assert payload['items'][0]['score'] >= payload['items'][1]['score']

    filtered = client.get('/api/admin/tenants/franchise-compare?status=basic', headers=headers)
    assert filtered.status_code == 200
    filtered_payload = filtered.get_json()
    assert filtered_payload['total'] >= 1
    assert all(item['status'] == 'basic' for item in filtered_payload['items'])
    assert any('currency_defined' in item['critical_gaps'] for item in filtered_payload['items'])
