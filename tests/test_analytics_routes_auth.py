
def test_api_analytics_report_generate_requires_auth_json(client):
    response = client.post('/api/analytics/report/generate', json={'tenant_id': 1, 'segment': 'municipio'})
    assert response.status_code == 401
    assert response.is_json
    payload = response.get_json() or {}
    assert payload.get('code') == 'auth_required'
    assert payload.get('request_id')


def test_api_analytics_report_latest_requires_auth_json(client):
    response = client.get('/api/analytics/report/latest', query_string={'tenant_id': 1})
    assert response.status_code == 401
    assert response.is_json
    payload = response.get_json() or {}
    assert payload.get('code') == 'auth_required'
    assert payload.get('request_id')


def test_api_analytics_benchmarks_requires_auth_json(client):
    response = client.get('/api/analytics/benchmarks', query_string={'tenant_id': 1})
    assert response.status_code == 401
    assert response.is_json
    payload = response.get_json() or {}
    assert payload.get('code') == 'auth_required'
    assert payload.get('request_id')


def test_analytics_runtime_preflight_contracts(client):
    origin = "https://www.chatboc.ar"
    paths = [
        "/api/analytics/report/latest",
        "/api/analytics/identity/coverage",
        "/api/admin/analytics/whatsapp-funnel",
        "/analytics/report/latest",
    ]
    for path in paths:
        response = client.open(
            path,
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, Content-Type, X-Request-Id",
            },
        )
        assert response.status_code == 200
        assert response.is_json
        assert response.headers.get("Access-Control-Allow-Origin") == origin
        assert response.headers.get("X-Request-Id")
