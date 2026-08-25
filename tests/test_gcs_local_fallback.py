import io
import os
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image
from flask import Flask, g
from werkzeug.datastructures import FileStorage

from services import gcs_service
from services.gcs_service import guardar_adjunto_y_thumbnail


@pytest.fixture(autouse=True)
def _disable_remote_storage(monkeypatch):
    monkeypatch.setattr(
        gcs_service.r2_service,
        "upload_file_with_key",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(gcs_service, "CLOUDINARY_ENABLED", False)
    monkeypatch.setattr(gcs_service, "VERCEL_BLOB_RW_TOKEN", None)
    monkeypatch.setattr(gcs_service, "GCS_ENABLED", False)
    monkeypatch.setattr(
        gcs_service,
        "_resolve_local_upload_base",
        lambda: gcs_service.current_app.config["LOCAL_UPLOAD_FOLDER"],
    )


def _make_image_file(name="test.jpg"):
    img = Image.new("RGB", (10, 10), color="red")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)
    return FileStorage(stream=buf, filename=name, content_type="image/jpeg")


def test_local_fallback(tmp_path):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)
    with app.app_context():
        with patch("services.gcs_service._get_gcs_client", side_effect=Exception("no gcs")):
            file_storage = _make_image_file()
            result = guardar_adjunto_y_thumbnail(file_storage)
    assert result is not None
    saved_path = upload_dir / result["unique_name"]
    assert saved_path.exists()
    assert result["original_url"].endswith(result["unique_name"])
    assert result["path"] == str(saved_path)


def test_local_fallback_uses_entity_folder(tmp_path):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)
    owner = SimpleNamespace(id=42, tipo_chat="municipio", municipio_id=77)

    with app.app_context():
        with app.test_request_context("/archivos/upload/chat_attachment"):
            g.owner_user = owner
            file_storage = _make_image_file("municipio.jpg")
            result = guardar_adjunto_y_thumbnail(file_storage)

    entity_dir = upload_dir / "municipio_77"
    assert entity_dir.exists()
    saved_path = entity_dir / result["unique_name"]
    assert saved_path.exists()
    assert result["path"] == str(saved_path)
    assert "municipio_77" in result["original_url"]


def test_local_fallback_purges_old_files(tmp_path):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)
    app.config["LOCAL_UPLOAD_RETENTION_DAYS"] = 1

    with app.app_context():
        with app.test_request_context("/archivos/upload/chat_attachment"):
            os.makedirs(upload_dir, exist_ok=True)
            old_file = upload_dir / "old_file.jpg"
            old_file.write_bytes(b"old")
            old_timestamp = datetime.utcnow() - timedelta(days=2)
            os.utime(
                old_file,
                (old_timestamp.timestamp(), old_timestamp.timestamp()),
            )

            file_storage = _make_image_file("nuevo.jpg")
            guardar_adjunto_y_thumbnail(file_storage)

    assert not old_file.exists()


def test_local_fallback_respects_forwarded_headers(tmp_path):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)

    with app.app_context():
        headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "api.chatboc.ar",
            "X-Forwarded-Port": "443",
        }
        with app.test_request_context("/archivos/upload/chat_attachment", headers=headers):
            file_storage = _make_image_file("forwarded.jpg")
            result = guardar_adjunto_y_thumbnail(file_storage)

    assert result["original_url"].startswith("https://api.chatboc.ar/")
    assert "forwarded" in result["original_url"]


def test_local_fallback_uses_backend_url_for_private_hosts(tmp_path):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)
    app.config["BACKEND_URL"] = "https://api.chatboc.ar"

    with app.app_context():
        with app.test_request_context(
            "/archivos/upload/chat_attachment",
            base_url="http://10.1.2.3:5000",
        ):
            file_storage = _make_image_file("privado.jpg")
            result = guardar_adjunto_y_thumbnail(file_storage)

    assert result["original_url"].startswith("https://api.chatboc.ar/")
    assert "privado" in result["original_url"]
    thumb_meta = result.get("thumb_meta") or {}
    assert thumb_meta.get("url", "").startswith("https://api.chatboc.ar/")


def test_local_fallback_fails_closed_on_vercel(tmp_path, monkeypatch):
    app = Flask(__name__)
    upload_dir = tmp_path / "uploads"
    app.config["LOCAL_UPLOAD_FOLDER"] = str(upload_dir)
    monkeypatch.setenv("VERCEL", "1")

    with app.app_context():
        with app.test_request_context("/archivos/upload/chat_attachment"):
            result = guardar_adjunto_y_thumbnail(_make_image_file("stateless.jpg"))

    assert result is None
    assert not upload_dir.exists()
