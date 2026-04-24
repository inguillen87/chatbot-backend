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
