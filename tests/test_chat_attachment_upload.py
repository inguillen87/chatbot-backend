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


def _response_json_and_status(response):
    return response.get_json(), response.status_code


class ChatAttachmentUploadTests(unittest.TestCase):
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

    def test_upload_chat_attachment_returns_thumbUrl(self):
        data = {"file": (BytesIO(b"fake"), "foto.png")}

        adjunto_mock = SimpleNamespace(
            id=1,
            url="/static/uploads/foto.png",
            mime="image/png",
            tamano=4,
            nombre_original="foto.png",
            filename="foto.png",
        )

        with self.app.test_request_context(
            "/archivos/upload/chat_attachment",
            method="POST",
            data=data,
            content_type="multipart/form-data",
            headers={"X-Chat-Session-Id": "abc"},
        ):
            with patch(
                "routes.archivos.create_attachment_with_thumbnail", return_value=adjunto_mock
            ):
                resp = archivos_route.upload_chat_attachment.__wrapped__(current_user=self.user)

        res_json, status = _response_json_and_status(resp)
        self.assertEqual(status, 200)
        info = res_json["attachmentInfo"]
        self.assertEqual(info["url"], adjunto_mock.url)
        self.assertEqual(info["downloadUrl"], adjunto_mock.url)
        self.assertEqual(info["storage_provider"], "external")
        self.assertEqual(info["storage_access"], "external")
        self.assertFalse(info["is_private"])
        self.assertIn("thumbUrl", info)
        self.assertEqual(info["thumbUrl"], "/static/uploads/foto_thumb.webp")
        self.assertEqual(info["thumbnailUrl"], info["thumbUrl"])
        self.assertEqual(info["meta"]["url"], info["thumbUrl"])

    def test_upload_chat_attachment_uses_meta_thumbUrl(self):
        data = {"file": (BytesIO(b"fake"), "foto.png")}

        from models import ArchivoAdjunto, AnalisisArchivo

        adjunto = ArchivoAdjunto(
            id=1,
            url="https://cdn.example.com/foto.png",
            mime="image/png",
            tamano=4,
            nombre_original="foto.png",
            filename="foto.png",
        )
        analisis = AnalisisArchivo(
            archivo_adjunto=adjunto,
            tipo_analisis="thumbnail_meta",
            datos_estructurados={"url": "https://cdn.example.com/foto_thumb.webp"},
        )
        db.session.add(adjunto)
        db.session.add(analisis)

        with self.app.test_request_context(
            "/archivos/upload/chat_attachment",
            method="POST",
            data=data,
            content_type="multipart/form-data",
            headers={"X-Chat-Session-Id": "abc"},
        ):
            with patch(
                "routes.archivos.create_attachment_with_thumbnail", return_value=adjunto
            ):
                resp = archivos_route.upload_chat_attachment.__wrapped__(current_user=self.user)

        res_json, status = _response_json_and_status(resp)
        self.assertEqual(status, 200)
        info = res_json["attachmentInfo"]
        self.assertEqual(info["thumbUrl"], "https://cdn.example.com/foto_thumb.webp")
        self.assertEqual(info["thumbnailUrl"], info["thumbUrl"])
        self.assertEqual(info["meta"]["url"], info["thumbUrl"])

    def test_upload_chat_attachment_allows_video_webm(self):
        data = {"file": (BytesIO(b"fake"), "nota.webm", "video/webm;codecs=opus")}

        adjunto_mock = SimpleNamespace(
            id=5,
            url="/static/uploads/nota.webm",
            mime="video/webm",
            tamano=4,
            nombre_original="nota.webm",
            filename="nota.webm",
        )

        with self.app.test_request_context(
            "/archivos/upload/chat_attachment",
            method="POST",
            data=data,
            content_type="multipart/form-data",
            headers={"X-Chat-Session-Id": "abc"},
        ):
            with patch(
                "routes.archivos.create_attachment_with_thumbnail",
                return_value=adjunto_mock,
            ):
                resp = archivos_route.upload_chat_attachment.__wrapped__(
                    current_user=self.user
                )

        _, status = _response_json_and_status(resp)
        self.assertEqual(status, 200)

    def test_ticket_comentario_to_dict_contains_thumbUrl(self):
        """Ensure model serialization uses local storage path when GCS is disabled."""
        from models import ArchivoAdjunto, TicketComentario

        adj = ArchivoAdjunto(
            filename="foto.png",
            nombre_original="foto.png",
            mime="image/png",
            tamano=4,
            url="/static/uploads/foto.png",
        )
        db.session.add(adj)
        db.session.commit()

        comentario = TicketComentario(
            comentario="hola",
            archivo_adjunto=adj,
        )
        db.session.add(comentario)
        db.session.commit()

        data = comentario.to_dict()
        self.assertIn("attachmentInfo", data)
        self.assertEqual(
            data["attachmentInfo"]["thumbUrl"],
            "/static/uploads/foto_thumb.webp",
        )
        self.assertEqual(
            data["attachmentInfo"]["thumbnailUrl"],
            data["attachmentInfo"]["thumbUrl"],
        )
        self.assertEqual(
            data["attachmentInfo"]["meta"]["url"],
            data["attachmentInfo"]["thumbUrl"],
        )

    def test_ticket_comentario_to_dict_uses_meta_thumbUrl(self):
        from models import ArchivoAdjunto, TicketComentario, AnalisisArchivo

        adj = ArchivoAdjunto(
            filename="foto.png",
            nombre_original="foto.png",
            mime="image/png",
            tamano=4,
            url="https://cdn.example.com/foto.png",
        )
        analisis = AnalisisArchivo(
            archivo_adjunto=adj,
            tipo_analisis="thumbnail_meta",
            datos_estructurados={"url": "https://cdn.example.com/foto_thumb.webp"},
        )
        db.session.add_all([adj, analisis])
        db.session.commit()

        comentario = TicketComentario(
            comentario="hola",
            archivo_adjunto=adj,
        )
        db.session.add(comentario)
        db.session.commit()

        data = comentario.to_dict()
        self.assertEqual(
            data["attachmentInfo"]["thumbUrl"],
            "https://cdn.example.com/foto_thumb.webp",
        )
        self.assertEqual(
            data["attachmentInfo"]["thumbnailUrl"],
            data["attachmentInfo"]["thumbUrl"],
        )
        self.assertEqual(
            data["attachmentInfo"]["meta"]["url"],
            data["attachmentInfo"]["thumbUrl"],
        )


if __name__ == "__main__":
    unittest.main()
