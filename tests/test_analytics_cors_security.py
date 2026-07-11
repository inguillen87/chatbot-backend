def test_analytics_api_rejects_untrusted_credentialed_origin(client):
    response = client.get(
        "/api/analytics/kpis?tenant_id=1",
        headers={"Origin": "https://evil.example"},
    )

    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_analytics_api_echoes_only_allowlisted_credentialed_origin(client):
    response = client.get(
        "/api/analytics/kpis?tenant_id=1",
        headers={"Origin": "https://www.chatboc.ar"},
    )

    assert response.headers.getlist("Access-Control-Allow-Origin") == ["https://www.chatboc.ar"]
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
    assert "*" not in response.headers.getlist("Access-Control-Allow-Origin")


def test_public_tracking_preflight_allows_header_credentials_not_url_secrets(client):
    response = client.options(
        "/api/public/tracking/experience?kind=claim&code=M-123456",
        headers={
            "Origin": "https://www.chatboc.ar",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "X-Tracking-Pin, X-Tracking-Token",
        },
    )

    allowed = response.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "x-tracking-pin" in allowed
    assert "x-tracking-token" in allowed
