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

        self.assertEqual(resp[1], 200)
        res_json = resp[0].get_json()
        info = res_json["attachmentInfo"]
        self.assertEqual(info["url"], adjunto_mock.url)
        self.assertIn("thumbUrl", info)
        self.assertEqual(info["thumbUrl"], "/static/uploads/foto_thumb.webp")

    def test_upload_chat_attachment_accepts_audio_webm(self):
        data = {"file": (BytesIO(b"fake"), "voz.webm", "audio/webm")}

        adjunto_mock = SimpleNamespace(
            id=1,
            url="/static/uploads/voz.webm",
            mime="audio/webm",
            tamano=4,
            nombre_original="voz.webm",
            filename="voz.webm",
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

        self.assertEqual(resp[1], 200)
        info = resp[0].get_json()["attachmentInfo"]
        self.assertEqual(info["mimeType"], "audio/webm")

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

        self.assertEqual(resp[1], 200)
        info = resp[0].get_json()["attachmentInfo"]
        self.assertEqual(info["thumbUrl"], "https://cdn.example.com/foto_thumb.webp")

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


if __name__ == "__main__":
    unittest.main()
