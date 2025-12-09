from types import SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError

from models import CatalogoItem
from routes import municipio_api


def test_legacy_productos_publicos_falls_back_when_column_missing(app, monkeypatch):
    tenant = SimpleNamespace(id=1, slug="demo")

    # First call raises ProgrammingError (simulating missing column),
    # second call succeeds and returns a single product.
    class QueryStub:
        def __init__(self):
            self.fail_next = True

        def options(self, *args, **kwargs):
            return self

        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            if self.fail_next:
                self.fail_next = False
                raise ProgrammingError("stmt", "params", None)
            return [SimpleNamespace(id=99, nombre="Producto demo")]

    with app.app_context():
        monkeypatch.setattr(municipio_api, "_resolve_tenant_or_404", lambda slug: tenant)
        monkeypatch.setattr(CatalogoItem, "query", QueryStub())
        monkeypatch.setattr(
            municipio_api, "serialize_catalogo_item", lambda prod: {"id": prod.id, "nombre": prod.nombre}
        )

        with app.test_request_context("/api/municipio/productos?tenant_slug=demo"):
            response = municipio_api.legacy_productos_publicos()

    assert response.get_json() == {"productos": [{"id": 99, "nombre": "Producto demo"}]}
