import pytest
from flask import Flask

from routes import productos
from routes.productos import productos_bp


class Obj:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def app(monkeypatch):
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test", WIDGET_URL="https://chatboc.ar")

    monkeypatch.setattr(productos, "ensure_seed_catalog", lambda *args, **kwargs: None)
    monkeypatch.setattr(productos, "_resolve_authenticated_user", lambda: None)
    monkeypatch.setattr(productos, "_resolve_public_owner", lambda: (Obj(slug="junin"), Obj(id=1)))

    app.register_blueprint(productos_bp)
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def test_redirect_preserves_tenant_slug_in_path(client):
    response = client.get("/productos?view=widget")

    assert response.status_code == 302
    assert response.headers["Location"] == "https://chatboc.ar/junin/productos?view=widget"
