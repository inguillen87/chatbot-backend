import os
import sys
import types
import importlib
import tempfile
import unittest
from io import BytesIO
from unittest.mock import patch
from werkzeug.datastructures import FileStorage
from flask import Flask


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
                gcs_service.refresh_cloudinary_configuration()
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
                gcs_service.refresh_cloudinary_configuration()
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

    def test_guardar_adjunto_desactiva_cloudinary_con_api_invalida(self):
        fake_cloudinary = types.ModuleType("cloudinary")
        fake_cloudinary.uploader = types.SimpleNamespace(upload=lambda *_, **__: {})
        fake_cloudinary.config = lambda **kwargs: None

        with patch.dict(sys.modules, {"cloudinary": fake_cloudinary}):
            with patch.dict(
                os.environ, {"CLOUDINARY_URL": "cloudinary://key:secret@test"}
            ):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)
                gcs_service.refresh_cloudinary_configuration()

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
                    "thumb_meta": {"width": 1, "height": 1, "url": "/static/uploads/local_foto_thumb.webp"},
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
                    side_effect=Exception("Unknown API key 123"),
                ) as mock_upload:
                    res_primero = gcs_service.guardar_adjunto_y_thumbnail(file_storage)
                    res_segundo = gcs_service.guardar_adjunto_y_thumbnail(file_storage)

        self.assertEqual(res_primero, fallback_payload)
        self.assertEqual(res_segundo, fallback_payload)
        self.assertEqual(mock_upload.call_count, 1)
        self.assertFalse(gcs_service.CLOUDINARY_ENABLED)
        self.assertEqual(gcs_service._CLOUDINARY_DISABLED_REASON, "unknown api key")
        self.assertEqual(mock_local.call_count, 2)

    def test_explicit_cloudinary_vars_override_url(self):
        fake_cloudinary = types.ModuleType("cloudinary")
        config_calls = []

        def fake_config(**kwargs):
            config_calls.append(kwargs)

        fake_cloudinary.uploader = types.SimpleNamespace(upload=lambda *_, **__: {})
        fake_cloudinary.config = fake_config

        env = {
            "CLOUDINARY_URL": "cloudinary://url-key:url-secret@demo",
            "CLOUDINARY_CLOUD_NAME": "real-cloud",
            "CLOUDINARY_API_KEY": "explicit-key",
            "CLOUDINARY_API_SECRET": "explicit-secret",
        }

        with patch.dict(sys.modules, {"cloudinary": fake_cloudinary}):
            with patch.dict(os.environ, env, clear=True):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)
                self.assertFalse(config_calls)
                gcs_service.refresh_cloudinary_configuration()

        self.assertTrue(config_calls)
        config_used = config_calls[-1]
        self.assertEqual(config_used["cloud_name"], "real-cloud")
        self.assertEqual(config_used["api_key"], "explicit-key")
        self.assertEqual(config_used["api_secret"], "explicit-secret")
        self.assertNotIn("cloudinary_url", config_used)

    def test_cloudinary_env_values_are_trimmed(self):
        fake_cloudinary = types.ModuleType("cloudinary")
        captured_config = {}

        def fake_config(**kwargs):
            captured_config.update(kwargs)

        fake_cloudinary.uploader = types.SimpleNamespace(upload=lambda *_, **__: {})
        fake_cloudinary.config = fake_config

        env = {
            "CLOUDINARY_CLOUD_NAME": " demo ",
            "CLOUDINARY_API_KEY": "  key  ",
            "CLOUDINARY_API_SECRET": " secret ",
            "CLOUDINARY_UPLOAD_FOLDER": " /nested/path/ ",
        }

        with patch.dict(sys.modules, {"cloudinary": fake_cloudinary}):
            with patch.dict(os.environ, env, clear=True):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)
                self.assertEqual(captured_config, {})
                gcs_service.refresh_cloudinary_configuration()

        self.assertEqual(captured_config["cloud_name"], "demo")
        self.assertEqual(captured_config["api_key"], "key")
        self.assertEqual(captured_config["api_secret"], "secret")

    def test_resolve_local_upload_base_migrates_existing_static_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Flask(__name__, root_path=tmpdir)
            static_uploads = os.path.join(tmpdir, "static", "uploads")
            os.makedirs(static_uploads)
            legacy_file = os.path.join(static_uploads, "legacy.txt")
            with open(legacy_file, "w", encoding="utf-8") as handler:
                handler.write("data")

            data_dir = os.path.join(tmpdir, "data")

            env = {"DATA_DIR": data_dir}
            with patch.dict(os.environ, env, clear=False):
                import services.gcs_service as gcs_service

                importlib.reload(gcs_service)

                with app.app_context():
                    resolved = gcs_service._resolve_local_upload_base()

            expected_static = os.path.join(tmpdir, "static", "uploads")
            self.assertEqual(resolved, expected_static)
            if os.path.islink(expected_static):
                target_path = os.readlink(expected_static)
                self.assertEqual(
                    os.path.abspath(target_path),
                    os.path.abspath(os.path.join(data_dir, "uploads")),
                )
            else:
                # Windows may deny symlink creation without Developer Mode.
                self.assertTrue(os.path.isdir(expected_static))
            migrated_file = os.path.join(data_dir, "uploads", "legacy.txt")
            self.assertTrue(os.path.exists(migrated_file))

    def test_local_upload_base_returns_static_when_symlink_fails(self):
        import services.gcs_service as gcs_service

        with tempfile.TemporaryDirectory() as tmp_root:
            data_root = os.path.join(tmp_root, "data-volume")
            default_dir = os.path.join(tmp_root, "static", "uploads")
            app = Flask(__name__, root_path=tmp_root)

            with patch.dict(os.environ, {"DATA_DIR": data_root}, clear=False):
                with patch("os.symlink", side_effect=OSError("permission denied")):
                    with app.app_context():
                        resolved = gcs_service._resolve_local_upload_base()

            self.assertEqual(resolved, default_dir)
            self.assertTrue(os.path.isdir(default_dir))
