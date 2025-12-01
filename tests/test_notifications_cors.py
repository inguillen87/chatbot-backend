def test_notifications_preflight_allows_tenant_headers(client, monkeypatch):
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    resp = client.options(
        "/notifications",
        headers={
            "Origin": "https://www.chatboc.ar",
            "Access-Control-Request-Headers": "x-tenant-id",
        },
    )

    assert resp.status_code == 200
    assert resp.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
    allow_headers = resp.headers.get("Access-Control-Allow-Headers", "").lower()
    assert "x-tenant-id" in allow_headers
    assert "x-tenant" in allow_headers
