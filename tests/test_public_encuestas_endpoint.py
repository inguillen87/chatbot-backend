from app import create_app, db
from config import TestingConfig
from routes.encuestas_public import _rate_buckets, _rate_limit


def test_public_encuestas_defaults_to_config_owner(client):
    response = client.get("/public/encuestas")
    assert response.status_code == 200
    assert response.get_json() == []
    assert "application/json" in response.headers.get("Content-Type", "")


def test_public_encuestas_options_is_handled(client):
    origin = "http://localhost:8080"
    response = client.options(
        "/public/encuestas", headers={"Origin": origin}
    )
    assert response.status_code == 204
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    allow_methods = response.headers.get("Access-Control-Allow-Methods", "")
    assert "OPTIONS" in allow_methods
    assert "GET" in allow_methods
    allow_headers = response.headers.get("Access-Control-Allow-Headers", "")
    assert "Content-Type" in allow_headers


def test_public_encuestas_get_includes_cors_headers(client):
    origin = "http://localhost:8080"
    response = client.get(
        "/public/encuestas", headers={"Origin": origin}
    )
    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"
    vary_header = response.headers.get("Vary", "")
    assert "Origin" in [item.strip() for item in vary_header.split(",") if item.strip()]


def test_not_found_returns_json(client):
    response = client.get("/ruta-inexistente")
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["error"] == "not_found"
    assert isinstance(payload.get("detail"), str)
    assert payload["detail"]


def test_internal_error_returns_json():
    local_app = create_app(TestingConfig)
    local_app.config.update(DEBUG=False, PROPAGATE_EXCEPTIONS=False)

    @local_app.route("/boom")
    def boom():  # pragma: no cover - simple runtime path
        raise RuntimeError("boom")

    with local_app.test_client() as test_client:
        with local_app.app_context():
            db.create_all()
            response = test_client.get("/boom")
            db.session.remove()
            db.drop_all()

    assert response.status_code == 500
    assert response.get_json() == {"error": "server_error"}


def test_public_encuestas_rate_limit_respects_config(app):
    ip = "203.0.113.10"
    with app.app_context():
        app.config["PUBLIC_ENCUESTAS_RATE_LIMIT"] = 2
        app.config["PUBLIC_ENCUESTAS_RATE_PERIOD"] = 60
        _rate_buckets.pop(ip, None)

        assert _rate_limit(ip) is True
        assert _rate_limit(ip) is True
        assert _rate_limit(ip) is False
