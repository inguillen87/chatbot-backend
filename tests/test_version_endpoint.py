from app import create_app
from config import TestingConfig


def test_version_endpoint_returns_configured_values():
    app = create_app(TestingConfig)
    app.config.update(
        FRONTEND_VERSION="frontend-build-test",
        BACKEND_VERSION="backend-build-test",
    )

    with app.test_client() as client:
        response = client.get("/api/version")

    assert response.status_code == 200
    assert response.get_json() == {
        "frontend": "frontend-build-test",
        "backend": "backend-build-test",
    }


def test_config_endpoint_exposes_version_info():
    app = create_app(TestingConfig)
    app.config.update(
        FRONTEND_VERSION="frontend-build-test",
        BACKEND_VERSION="backend-build-test",
    )

    with app.test_client() as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    data = response.get_json()
    assert data["frontendVersion"] == "frontend-build-test"
    assert data["backendVersion"] == "backend-build-test"
