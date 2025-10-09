from app import create_app, db
from config import TestingConfig


def test_public_encuestas_defaults_to_config_owner(client):
    response = client.get("/public/encuestas")
    assert response.status_code == 200
    assert response.get_json() == []
    assert "application/json" in response.headers.get("Content-Type", "")


def test_public_encuestas_options_is_handled(client):
    response = client.options("/public/encuestas")
    assert response.status_code == 204


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
