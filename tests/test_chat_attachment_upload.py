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

    def test_upload_chat_attachment_hides_unmapped_local_original_and_thumbnail(self):
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
        self.assertIsNone(info["url"])
        self.assertIsNone(info["downloadUrl"])
        self.assertEqual(info["storage_access"], "unavailable")
        self.assertEqual(info['reason_code'], 'attachment_private_migration_required')
        self.assertNotIn(adjunto_mock.url, str(info))
        self.assertFalse(info["is_private"])
        self.assertIsNone(info.get('thumbUrl'))
        self.assertIsNone(info.get('thumbnailUrl'))
        self.assertIsNone(info['meta']['url'])

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
        self.assertIsNone(info.get('thumbUrl'))
        self.assertIsNone(info.get('thumbnailUrl'))
        self.assertIsNone(info['meta']['url'])
        self.assertNotIn('https://cdn.example.com/', str(info))

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

    def test_upload_chat_attachment_returns_413_before_storage_or_db(self):
        data = {"file": (BytesIO(b"12345"), "foto.png", "image/png")}

        with self.app.test_request_context(
            "/archivos/upload/chat_attachment",
            method="POST",
            data=data,
            content_type="multipart/form-data",
            headers={
                "X-Chat-Session-Id": "abc",
                "X-Request-Id": "oversize-request",
            },
        ):
            with patch.object(
                archivos_route,
                "STORAGE_MAX_FILE_SIZE",
                4,
            ), patch(
                "routes.archivos.create_attachment_with_thumbnail"
            ) as create_attachment_mock, patch.object(
                db.session,
                "commit",
            ) as commit_mock:
                resp = archivos_route.upload_chat_attachment.__wrapped__(
                    current_user=self.user
                )

        res_json, status = _response_json_and_status(resp)
        self.assertEqual(status, 413)
        self.assertEqual(res_json["contract_version"], "upload.error.v1")
        self.assertEqual(res_json["code"], "file_too_large")
        self.assertEqual(res_json["max_file_bytes"], 4)
        self.assertEqual(res_json["request_id"], "oversize-request")
        create_attachment_mock.assert_not_called()
        commit_mock.assert_not_called()

    def test_upload_chat_attachment_caps_multipart_before_auth_decorator_parse(self):
        client = self.app.test_client()
        data = {
            "file": (
                BytesIO(b"x" * (1024 * 1024 + 1024)),
                "oversize.png",
                "image/png",
            )
        }

        with patch.object(
            archivos_route,
            "STORAGE_MAX_FILE_SIZE",
            4,
        ), patch(
            "routes.archivos.create_attachment_with_thumbnail"
        ) as create_attachment_mock, patch.object(
            db.session,
            "commit",
        ) as commit_mock:
            response = client.post(
                "/archivos/upload/chat_attachment",
                data=data,
                content_type="multipart/form-data",
                headers={"X-Request-Id": "early-parser-cap"},
            )

        payload = response.get_json() or {}
        self.assertEqual(response.status_code, 413)
        self.assertEqual(payload.get("contract_version"), "upload.error.v1")
        self.assertEqual(payload.get("code"), "file_too_large")
        self.assertEqual(payload.get("request_id"), "early-parser-cap")
        create_attachment_mock.assert_not_called()
        commit_mock.assert_not_called()

    def test_ticket_comentario_to_dict_contains_thumbUrl(self):
        """Unmapped legacy paths must not be emitted by model serialization."""
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
        self.assertIsNone(data['attachmentInfo']['url'])
        self.assertIsNone(data['attachmentInfo'].get('thumbUrl'))
        self.assertIsNone(data['attachmentInfo']['meta']['url'])
        self.assertNotIn('/static/uploads/', str(data))

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
        self.assertIsNone(data['attachmentInfo'].get('thumbUrl'))
        self.assertIsNone(data['attachmentInfo']['meta']['url'])
        self.assertNotIn('https://cdn.example.com/', str(data))


if __name__ == "__main__":
    unittest.main()
