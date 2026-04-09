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
        db.session.add(self.user)
        db.session.commit()

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

