"""Real create_app/static routing with isolated startup and no database I/O."""
import mimetypes
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Response
from sqlalchemy.engine import Engine

import app as application
from config import TestingConfig


@pytest.fixture(params=("missing", "wrong"))
def static_app(monkeypatch, request):
    # Model slim runtime tables deterministically, independent of Windows MIME
    # registry entries. The application must supply this strict media type.
    mimetypes.init()
    if request.param == "missing":
        monkeypatch.delitem(mimetypes.types_map, ".webp", raising=False)
    else:
        monkeypatch.setitem(mimetypes.types_map, ".webp", "application/octet-stream")
    monkeypatch.setattr(application, "MIGRATIONS_ONLY", True)

    class StaticOnlyConfig(TestingConfig):
        SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
        RATELIMIT_STORAGE_URI = "memory://"

    # Use the real factory and built-in static view. Migration startup skips
    # integrations; additionally forbid any SQLAlchemy connection throughout.
    with patch.object(Engine, "connect", side_effect=AssertionError("No database I/O in static MIME fixture")) as connect:
        current = application.create_app(StaticOnlyConfig)
        current.add_url_rule(
            "/mime-scope.webp",
            "mime_scope_control",
            lambda: Response(b"opaque-control", mimetype="application/octet-stream"),
        )
        yield current
        connect.assert_not_called()


def test_static_webp_has_explicit_mime(static_app):
    expected = (Path(static_app.static_folder) / "welcome/juni-saludo-sticker.webp").read_bytes()
    client = static_app.test_client()
    for method in ("GET", "HEAD"):
        response = client.open("/static/welcome/juni-saludo-sticker.webp", method=method)
        assert response.status_code == 200
        assert response.mimetype == "image/webp"
        assert response.headers["Content-Length"] == str(len(expected))
        assert response.data == (expected if method == "GET" else b"")
        assert "Set-Cookie" not in response.headers


def test_static_png_keeps_its_mime_and_bytes(static_app):
    path = "municipios/default/logo-junin-punto-limpio-1024x472.png"
    expected = (Path(static_app.static_folder) / path).read_bytes()
    client = static_app.test_client()
    for method in ("GET", "HEAD"):
        response = client.open("/static/" + path, method=method)
        assert response.status_code == 200
        assert response.mimetype == "image/png"
        assert response.headers["Content-Length"] == str(len(expected))
        assert response.data == (expected if method == "GET" else b"")


def test_static_webp_preserves_conditional_response(static_app):
    client = static_app.test_client()
    first = client.get("/static/welcome/juni-saludo-sticker.webp")
    assert first.status_code == 200
    response = client.get(
        "/static/welcome/juni-saludo-sticker.webp",
        headers={"If-None-Match": first.headers["ETag"]},
    )
    assert response.status_code == 304
    assert response.data == b""
    assert response.headers["ETag"] == first.headers["ETag"]


def test_static_webp_preserves_range_response(static_app):
    response = static_app.test_client().get(
        "/static/welcome/juni-saludo-sticker.webp",
        headers={"Range": "bytes=0-11"},
    )
    assert response.status_code == 206
    assert response.mimetype == "image/webp"
    assert response.data[:4] == b"RIFF"
    assert response.data[8:12] == b"WEBP"
    assert response.headers["Content-Range"].startswith("bytes 0-11/")


def test_missing_webp_stays_an_error_not_an_image(static_app):
    response = static_app.test_client().get("/static/welcome/synthetic-missing.webp")
    assert response.status_code == 404
    assert response.mimetype == "application/json"
    assert response.get_json()["reason_code"] == "not_found"


def test_nonstatic_explicit_mime_is_unchanged(static_app):
    response = static_app.test_client().get("/mime-scope.webp")
    assert response.status_code == 200
    assert response.mimetype == "application/octet-stream"
    assert response.data == b"opaque-control"
