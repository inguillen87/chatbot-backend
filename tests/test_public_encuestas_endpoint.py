from app import create_app, db
from config import TestingConfig
from routes.encuestas_public import _public_error_response, _rate_buckets, _rate_limit
from services.encuestas_service import EncuestaError


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
    assert response.headers.get("Access-Control-Allow-Credentials") is None
    vary_header = response.headers.get("Vary", "")
    assert "Origin" in [item.strip() for item in vary_header.split(",") if item.strip()]


def test_not_found_returns_json(client):
    response = client.get("/ruta-inexistente")
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["status_code"] == 404
    assert payload["reason_code"] == "not_found"
    assert payload["error"]["code"] == 404
    assert isinstance(payload.get("detail"), str)
    assert payload["detail"]
    assert payload["request_id"]
    assert response.headers.get("X-Request-Id") == payload["request_id"]


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
    payload = response.get_json()
    assert payload["contract_version"] == "shared.error.v1"
    assert payload["status_code"] == 500
    assert payload["reason_code"] == "server_error"
    assert payload["error"]["message"] == "Internal server error"
    assert payload["request_id"]


def test_public_encuestas_rate_limit_respects_config(app):
    ip = "203.0.113.10"
    with app.app_context():
        app.config["PUBLIC_ENCUESTAS_RATE_LIMIT"] = 2
        app.config["PUBLIC_ENCUESTAS_RATE_PERIOD"] = 60
        _rate_buckets.pop(ip, None)

        assert _rate_limit(ip) is True
        assert _rate_limit(ip) is True
        assert _rate_limit(ip) is False


def test_public_survey_error_response_has_reason_and_request_id(app):
    with app.test_request_context(
        "/public/encuestas/demo",
        headers={"X-Request-Id": "req-survey-1"},
    ):
        response, status = _public_error_response(
            EncuestaError(
                "La encuesta no está activa",
                status_code=403,
                payload={"reason_code": "survey_not_published"},
            )
        )

    assert status == 403
    payload = response.get_json()
    assert payload["contract_version"] == "encuestas.public_error.v1"
    assert payload["reason_code"] == "survey_not_published"
    assert payload["retryable"] is False
    assert payload["action_hint"] == "view_other_surveys"
    assert payload["request_id"] == "req-survey-1"
    assert response.headers["X-Request-Id"] == "req-survey-1"


def test_public_survey_v1_missing_slug_returns_contract(client):
    response = client.get("/api/public/encuestas/v1/no-existe")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["contract_version"] == "public.survey_resolution.v1"
    assert payload["reason_code"] == "survey_not_found"
    assert payload["retryable"] is False
    assert payload["list_endpoint"] == "/api/public/encuestas"
    assert payload["status_code"] == 404
    assert payload["action_hint"] == "go_home"
    assert payload["request_id"]
