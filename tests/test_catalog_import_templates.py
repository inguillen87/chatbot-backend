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

    tenant = Obj(id=1, configuracion={})
    owner = Obj(id=2)

    monkeypatch.setattr(
        catalog_import,
        "resolve_tenant_and_user",
        lambda tenant_slug=None, current_user=None: (tenant, owner, None),
    )
    monkeypatch.setattr(catalog_import, "_persist_rows", lambda *args, **kwargs: 1)
    monkeypatch.setattr(catalog_import, "extract_table_from_file", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        catalog_import.pd, "read_excel", lambda *args, **kwargs: catalog_import.pd.DataFrame()
    )
    monkeypatch.setattr(
        catalog_import.pd, "read_csv", lambda *args, **kwargs: catalog_import.pd.DataFrame()
    )

    app.register_blueprint(catalog_import_bp)
    app.tenant = tenant  # type: ignore[attr-defined]
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_column_map_applied_and_saved(client, app, monkeypatch):
    captured_rows = {}

    def _persist(owner_id, tenant_id, rows):
        captured_rows["rows"] = rows
        return len(rows)

    monkeypatch.setattr(catalog_import, "_persist_rows", _persist)
    monkeypatch.setattr(
        catalog_import, "extract_table_from_file", lambda *args, **kwargs: [{"titulo": "A"}]
    )

    resp = client.post(
        "/api/admin/catalogo/importar",
        data={
            "archivo": (io.BytesIO(b"data"), "catalogo.pdf"),
            "column_map": "{\"titulo\": \"nombre\"}",
            "plantilla": "default",
            "guardar_plantilla": "true",
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json()["plantilla_aplicada"] == "default"
    assert captured_rows["rows"][0]["nombre"] == "A"
    assert app.tenant.configuracion["catalogo_import_templates"]["default"]["titulo"] == "nombre"


def test_template_loaded_when_column_map_missing(client, app, monkeypatch):
    app.tenant.configuracion = {
        "catalogo_import_templates": {"mi_plantilla": {"titulo": "nombre"}}
    }

    captured_rows = {}

    def _persist(owner_id, tenant_id, rows):
        captured_rows["rows"] = rows
        return len(rows)

    monkeypatch.setattr(catalog_import, "_persist_rows", _persist)
    monkeypatch.setattr(
        catalog_import, "extract_table_from_file", lambda *args, **kwargs: [{"titulo": "B"}]
    )

    resp = client.post(
        "/api/admin/catalogo/importar",
        data={"archivo": (io.BytesIO(b"data"), "catalogo.pdf"), "plantilla": "mi_plantilla"},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 200
    assert resp.is_json
    assert resp.get_json()["plantilla_aplicada"] == "mi_plantilla"
    assert captured_rows["rows"][0]["nombre"] == "B"


def test_import_preserves_image_columns_for_marketplace(client, monkeypatch):
    captured_rows = {}

    def _persist(owner_id, tenant_id, rows):
        captured_rows["rows"] = rows
        return len(rows)

    monkeypatch.setattr(catalog_import, "_persist_rows", _persist)
    monkeypatch.setattr(
        catalog_import,
        "extract_table_from_file",
        lambda *args, **kwargs: [
            {
                "titulo": "Campera escolar",
                "imagen": "https://cdn.example.com/campera.jpg",
                "imagenes": "https://cdn.example.com/campera.jpg; https://cdn.example.com/detalle.jpg",
            }
        ],
    )

    resp = client.post(
        "/api/admin/catalogo/importar",
        data={
            "archivo": (io.BytesIO(b"data"), "catalogo.pdf"),
            "column_map": "{\"titulo\": \"nombre\", \"imagen\": \"imagen_url\", \"imagenes\": \"gallery_urls\"}",
        },
        content_type="multipart/form-data",
    )

    body = resp.get_json()
    assert resp.status_code == 200
    assert body["image_summary"]["with_images"] == 1
    assert captured_rows["rows"][0]["imagen_url"] == "https://cdn.example.com/campera.jpg"
    assert captured_rows["rows"][0]["gallery_urls"] == [
        "https://cdn.example.com/campera.jpg",
        "https://cdn.example.com/detalle.jpg",
    ]


def test_import_preview_contract_is_editable_and_manual_commit(app):
    upload = catalog_import.CatalogUpload(
        id=77,
        tenant_id=1,
        filename="catalogo.csv",
        mime_type="text/csv",
        status="ready_to_commit",
        processor_slug="generic_v2",
        engine_used="test",
        preview_data={
            "items": [
                {
                    "nombre": "Producto listo",
                    "precio": "1200",
                    "stock": "5",
                    "descripcion": "Descripcion corta",
                    "imagen_url": "https://cdn.example.com/p.jpg",
                },
                {"nombre": "Producto incompleto"},
            ]
        },
        warnings=[],
    )

    with app.test_request_context("/api/admin/catalog/import/77", headers={"X-Request-Id": "import-77"}):
        payload = catalog_import._catalog_import_preview_contract(upload)

    assert payload["contract_version"] == "catalog.import_preview.v1"
    assert payload["request_id"] == "import-77"
    assert payload["commit_endpoint"] == "/api/admin/catalog/import/77/commit"
    assert payload["publish_policy"] == "manual_commit_required"
    assert payload["frontend_contract"]["editable_rows"] is True
    assert payload["quality_summary"]["ready_to_publish"] == 1
    assert payload["quality_summary"]["without_price"] == 1
    assert payload["image_summary"]["with_images"] == 1
    assert payload["rows_sample"][1]["warnings"]
    assert any(action["id"] == "review_prices" for action in payload["suggested_actions"])
