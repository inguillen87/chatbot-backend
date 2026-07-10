import io

import pytest
from flask import Flask

from routes import catalog_import
from routes.catalog_import import catalog_import_bp


class Obj:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test")

    monkeypatch.setattr(
        catalog_import,
        "resolve_tenant_and_user",
        lambda tenant_slug=None, current_user=None: (
            Obj(id=1, plan="full", is_active=True, configuracion={}),
            Obj(id=2),
            None,
        ),
    )
    monkeypatch.setattr(catalog_import, "_persist_rows", lambda *args, **kwargs: 0)
    app.register_blueprint(catalog_import_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_missing_file_returns_json(client):
    response = client.post("/api/admin/catalogo/importar")

    assert response.status_code == 400
    assert response.is_json
    assert response.get_json()["codigo"] == "archivo_requerido"
    assert response.headers["Content-Type"].startswith("application/json")


def test_free_plan_can_import_catalog_as_self_service_module(client, monkeypatch):
    processed = {"called": False}

    monkeypatch.setattr(
        catalog_import,
        "resolve_tenant_and_user",
        lambda tenant_slug=None, current_user=None: (
            Obj(id=1, plan="free", is_active=True, configuracion={}),
            Obj(id=2),
            None,
        ),
    )

    def _parse_catalog(*args, **kwargs):
        processed["called"] = True
        return [{"titulo": "Producto", "precio": "100"}]

    monkeypatch.setattr(catalog_import, "extract_table_from_file", _parse_catalog)

    response = client.post(
        "/api/admin/catalogo/importar",
        data={"archivo": (io.BytesIO(b"data"), "catalogo.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 200
    assert response.is_json
    body = response.get_json()
    assert body["ok"] is True
    assert body["filas_detectadas"] == 1
    assert processed["called"] is True


def test_demo_tenant_cannot_import_catalog_before_processing(client, monkeypatch):
    processed = {"called": False}

    monkeypatch.setattr(
        catalog_import,
        "resolve_tenant_and_user",
        lambda tenant_slug=None, current_user=None: (
            Obj(id=1, plan="full", is_active=True, configuracion={"demo_mode": True}),
            Obj(id=2),
            None,
        ),
    )

    def _should_not_parse(*args, **kwargs):
        processed["called"] = True
        return [{"titulo": "Producto"}]

    monkeypatch.setattr(catalog_import, "extract_table_from_file", _should_not_parse)

    response = client.post(
        "/api/admin/catalogo/importar",
        data={"archivo": (io.BytesIO(b"data"), "catalogo.pdf")},
        content_type="multipart/form-data",
    )

    body = response.get_json()
    assert response.status_code == 403
    assert response.is_json
    assert body["error"] == "plan_required"
    assert body["feature"]["id"] == "catalog_management"
    assert body["access"]["features"]["catalog_management"]["enabled"] is False
    assert body["lock_reason_code"] == "demo_tenant_locked"
    assert processed["called"] is False


def test_unsupported_format_returns_json(client, monkeypatch):
    monkeypatch.setattr(catalog_import, "extract_table_from_file", lambda *args, **kwargs: [])

    def _fail_excel(*args, **kwargs):
        raise ValueError("excel fail")

    def _fail_csv(*args, **kwargs):
        raise ValueError("csv fail")

    monkeypatch.setattr(catalog_import.pd, "read_excel", _fail_excel)
    monkeypatch.setattr(catalog_import.pd, "read_csv", _fail_csv)

    response = client.post(
        "/api/admin/catalogo/importar",
        data={"archivo": (io.BytesIO(b"invalid"), "nota.txt")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.is_json
    body = response.get_json()
    assert body["codigo"] == "formato_no_soportado"
    assert "formato" in body["mensaje"].lower()
    assert response.headers["Content-Type"].startswith("application/json")


def test_llm_parsing_error_returns_json(client, monkeypatch):
    def _explode(*args, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(catalog_import, "extract_table_from_file", _explode)

    response = client.post(
        "/api/admin/catalogo/importar",
        data={"archivo": (io.BytesIO(b"datos"), "pedido.pdf")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.is_json
    body = response.get_json()
    assert body["codigo"] == "formato_no_soportado"
    assert "no se pudo" in body["mensaje"].lower()
    assert response.headers["Content-Type"].startswith("application/json")


def test_method_not_allowed_returns_json(client):
    response = client.get("/api/admin/catalogo/importar")

    assert response.status_code == 405
    assert response.is_json
    body = response.get_json()
    assert body["codigo"] == "method_not_allowed"
    assert "method" in body["mensaje"].lower()
    assert response.headers["Content-Type"].startswith("application/json")


def test_unsupported_method_returns_json_not_html(client):
    response = client.put("/api/admin/catalogo/importar")

    assert response.status_code == 405
    assert response.is_json
    assert response.get_json()["codigo"] == "method_not_allowed"
