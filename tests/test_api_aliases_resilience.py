from routes import api_aliases


def test_tenants_alias_list_graceful_on_exception(client, monkeypatch):
    monkeypatch.setattr(api_aliases, 'list_followed_tenants', lambda: (_ for _ in ()).throw(RuntimeError('db down')) )
    response = client.get('/api/app/me/tenants')
    assert response.status_code == 200
    assert response.get_json() == []


def test_admin_login_alias_graceful_on_exception(client, monkeypatch):
    monkeypatch.setattr(api_aliases, 'admin_login', lambda: (_ for _ in ()).throw(RuntimeError('upstream down')) )
    response = client.post('/api/auth/admin/login', json={'email': 'x', 'password': 'y'})
    assert response.status_code == 503
    payload = response.get_json()
    assert payload.get('reason_code') == 'auth_service_unavailable'


def test_demo_catalog_alias_graceful_on_exception(client, monkeypatch):
    monkeypatch.setattr(api_aliases, 'demo_catalog', lambda: (_ for _ in ()).throw(RuntimeError('catalog down')) )
    response = client.get('/api/auth/demo/catalog')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload.get('reason_code') == 'demo_catalog_unavailable'
    assert payload.get('tenant_demos') == []


def test_pwa_tenant_info_alias_graceful_on_exception(client, monkeypatch):
    monkeypatch.setattr(api_aliases, 'tenant_profile', lambda: (_ for _ in ()).throw(RuntimeError('tenant resolver down')) )
    response = client.get('/api/pwa/tenant-info?tenant_slug=municipio')
    assert response.status_code == 200
    payload = response.get_json()
    assert payload.get('reason_code') == 'tenant_info_unavailable'
