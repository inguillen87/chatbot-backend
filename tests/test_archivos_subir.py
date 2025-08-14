import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from app import create_app, db
from config import Config
from models import User
from routes import archivos as archivos_route


class TestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    WTF_CSRF_ENABLED = False


class ArchivosSubirTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()
        self.user = User(rol="usuario", name="Test", email="t@example.com", password_hash="hash")
        db.session.add(self.user)
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_subir_archivo_single_field(self):
        dummy_task = SimpleNamespace(delay=lambda *a, **k: None)
        mock_upload = {
            "unique_name": "fake.txt",
            "public_url": "http://example.com/fake.txt",
            "size": 5,
            "original_name": "fake.txt",
            "mimetype": "text/plain",
        }

        data = {
            "archivo": (BytesIO(b"hello"), "fake.txt"),
        }

        with self.app.test_request_context(
            "/archivos/subir", method="POST", data=data, content_type="multipart/form-data"
        ):
            with patch("routes.archivos.upload_to_gcs", return_value=mock_upload), \
                patch("routes.archivos.procesar_catalogo_pdf_google", lambda *a, **k: []), \
                patch("routes.archivos.procesar_catalogo_imagen_google", lambda *a, **k: []), \
                patch("routes.archivos.tarea_analizar_contenido_archivo", dummy_task, create=True):
                resp = archivos_route.subir_archivo.__wrapped__(self.user)

        self.assertEqual(resp[1], 200)
        res_json = resp[0].get_json()
        self.assertEqual(res_json["name"], "fake.txt")


if __name__ == "__main__":
    unittest.main()
