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
