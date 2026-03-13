
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
