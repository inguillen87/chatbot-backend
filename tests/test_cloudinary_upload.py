import os
import sys
import types
import importlib
import unittest
from io import BytesIO
from unittest.mock import patch
from werkzeug.datastructures import FileStorage


class CloudinaryUploadTests(unittest.TestCase):
    def test_guardar_adjunto_uses_cloudinary(self):
        fake_cloudinary = types.ModuleType("cloudinary")
        fake_cloudinary.uploader = types.SimpleNamespace(upload=lambda *_, **__: {})
        fake_cloudinary.config = lambda **kwargs: None
        with patch.dict(sys.modules, {"cloudinary": fake_cloudinary}):
            with patch.dict(
                os.environ, {"CLOUDINARY_URL": "cloudinary://key:secret@test"}
            ):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)
                file_storage = FileStorage(
                    stream=BytesIO(b"img"),
                    filename="foto.png",
                    content_type="image/png",
                )
                with patch(
                    "services.gcs_service.generar_thumbnail",
                    return_value=(b"thumb", {"width": 1, "height": 1}),
                ), patch("services.gcs_service.uploader.upload") as mock_upload:
                    mock_upload.side_effect = [
                        {
                            "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/foto.png"
                        },
                        {
                            "secure_url": "https://res.cloudinary.com/demo/image/upload/v1/foto_thumb.webp"
                        },
                    ]
                    res = gcs_service.guardar_adjunto_y_thumbnail(file_storage)
        self.assertEqual(
            res["original_url"],
            "https://res.cloudinary.com/demo/image/upload/v1/foto.png",
        )
        self.assertEqual(
            res["thumb_meta"]["url"],
            "https://res.cloudinary.com/demo/image/upload/v1/foto_thumb.webp",
        )

    def test_guardar_adjunto_cloudinary_failure_falls_back(self):
        fake_cloudinary = types.ModuleType("cloudinary")
        fake_cloudinary.uploader = types.SimpleNamespace(upload=lambda *_, **__: {})
        fake_cloudinary.config = lambda **kwargs: None
        with patch.dict(sys.modules, {"cloudinary": fake_cloudinary}):
            with patch.dict(
                os.environ, {"CLOUDINARY_URL": "cloudinary://key:secret@test"}
            ):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)
                file_storage = FileStorage(
                    stream=BytesIO(b"img"),
                    filename="foto.png",
                    content_type="image/png",
                )
                fallback_payload = {
                    "unique_name": "local_foto.png",
                    "original_url": "/static/uploads/local_foto.png",
                    "size": 3,
                    "original_name": "foto.png",
                    "mimetype": "image/png",
                    "thumb_meta": {
                        "width": 1,
                        "height": 1,
                        "url": "/static/uploads/local_foto_thumb.webp",
                    },
                    "thumbUrl": "/static/uploads/local_foto_thumb.webp",
                }
                with patch(
                    "services.gcs_service.generar_thumbnail",
                    return_value=(b"thumb", {"width": 1, "height": 1}),
                ), patch(
                    "services.gcs_service._save_to_local",
                    return_value=fallback_payload,
                ) as mock_local, patch(
                    "services.gcs_service.uploader.upload",
                    side_effect=Exception("bad key"),
                ):
                    res = gcs_service.guardar_adjunto_y_thumbnail(file_storage)

        mock_local.assert_called_once()
        self.assertEqual(res, fallback_payload)
