import io
import unittest
from werkzeug.datastructures import FileStorage
from unittest.mock import patch

from app import create_app
from config import TestConfig
from models import db, User, ArchivoAdjunto
from services.upload_processor import _store_catalog_attachment_record


class UploadProcessorCatalogAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()

        self.user = User(email="catalog-upload@test.com", name="Catalog Upload", rol="admin", tipo_chat="pyme")
        self.user.set_password("test")
        self.user.token = "token-catalog-upload-test"
        db.session.add(self.user)
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    @patch("services.upload_processor.upload_to_gcs")
    def test_store_catalog_attachment_record_persists_cdn_url(self, mock_upload_to_gcs):
        mock_upload_to_gcs.return_value = {
            "unique_name": "abc_catalogo.pdf",
            "public_url": "https://cdn.chatboc.ar/pymes/bodega/attachments/abc_catalogo.pdf",
            "size": 1234,
            "original_name": "catalogo.pdf",
            "mimetype": "application/pdf",
        }

        file_storage = FileStorage(
            stream=io.BytesIO(b"%PDF-1.4 fake"),
            filename="catalogo.pdf",
            content_type="application/pdf",
        )

        adjunto = _store_catalog_attachment_record(file_storage, self.user)
        db.session.commit()

        self.assertIsNotNone(adjunto)
        self.assertEqual(adjunto.tipo, "catalogo")
        self.assertEqual(adjunto.url, "https://cdn.chatboc.ar/pymes/bodega/attachments/abc_catalogo.pdf")

        persisted = ArchivoAdjunto.query.filter_by(user_id=self.user.id, tipo="catalogo").first()
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.url, adjunto.url)

    @patch("services.upload_processor.procesar_y_embedear_catalogo")
    @patch("services.upload_processor._store_catalog_attachment_record")
    def test_subir_catalogo_continua_si_falla_storage_url(self, mock_store_attachment, mock_process_catalog):
        mock_store_attachment.return_value = None
        mock_process_catalog.return_value = 3

        data = {
            "file": (io.BytesIO(b"columna,precio\nmalbec,43200\n"), "catalogo.csv"),
        }
        response = self.client.post(
            "/subir_catalogo",
            data=data,
            headers={"Authorization": f"Bearer {self.user.token}"},
            content_type="multipart/form-data",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("items_count"), 3)
        self.assertIn("upload_id", payload)
        self.assertIsNone(payload.get("catalog_url"))
        self.assertIsNone(payload.get("catalog_attachment_id"))
        mock_process_catalog.assert_called_once()

    @patch("services.upload_processor.procesar_y_embedear_catalogo")
    @patch("services.upload_processor._store_catalog_attachment_record")
    def test_subir_catalogo_rejects_oversize_before_storage_or_processing(
        self,
        mock_store_attachment,
        mock_process_catalog,
    ):
        with patch("services.upload_processor.CATALOG_UPLOAD_MAX_BYTES", 4):
            response = self.client.post(
                "/subir_catalogo",
                data={"file": (io.BytesIO(b"12345"), "catalogo.csv")},
                headers={"Authorization": f"Bearer {self.user.token}"},
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 413)
        payload = response.get_json() or {}
        self.assertEqual(payload.get("code"), "file_too_large")
        self.assertEqual(payload.get("max_file_bytes"), 4)
        mock_store_attachment.assert_not_called()
        mock_process_catalog.assert_not_called()
        self.assertEqual(ArchivoAdjunto.query.count(), 0)
