import io
import os
from PIL import Image
from werkzeug.datastructures import FileStorage
from unittest.mock import patch
from flask import Flask

from services.gcs_service import guardar_adjunto_y_thumbnail


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
