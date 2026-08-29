from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import re
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from flask import g
from werkzeug.datastructures import FileStorage

import services.gcs_service as gcs_service
from services.r2_service import R2Service
from services.attachment_delivery import serialize_attachment_for_delivery
import services.attachment_delivery as attachment_delivery


class _RecordingS3Client:
    def __init__(self):
        self.calls = []
        self.presigned_calls = []

    def upload_fileobj(self, file_obj, bucket_name, key, ExtraArgs=None):
        self.calls.append(
            {
                "file_obj": file_obj,
                "bucket_name": bucket_name,
                "key": key,
                "extra_args": dict(ExtraArgs or {}),
            }
        )

    def generate_presigned_url(self, operation, Params=None, ExpiresIn=None):
        self.presigned_calls.append(
            {
                "operation": operation,
                "params": dict(Params or {}),
                "expires_in": ExpiresIn,
            }
        )
        return f"https://r2-signed.example/{Params['Key']}?ttl={ExpiresIn}"


def _configured_service(client):
    with patch.dict("os.environ", {}, clear=True):
        service = R2Service()
    service.client = client
    service.bucket_name = "chatboc-assets"
    service.public_base_url = "https://cdn.chatboc.ar"
    return service


def test_r2_lazy_client_initialization_waits_for_concurrent_first_use():
    service = R2Service()
    service.endpoint_url = "https://r2.example.invalid"
    service.access_key_id = "test-access-key"
    service.secret_access_key = "test-secret-key"
    service.bucket_name = "chatboc-assets"

    expected_client = object()
    initialization_started = Event()
    release_initialization = Event()
    create_calls = 0

    def create_client():
        nonlocal create_calls
        create_calls += 1
        initialization_started.set()
        assert release_initialization.wait(timeout=5)
        return expected_client

    service._create_client = create_client

    with ThreadPoolExecutor(max_workers=20) as executor:
        first = executor.submit(service._get_client)
        assert initialization_started.wait(timeout=5)
        followers = [executor.submit(service._get_client) for _ in range(19)]
        release_initialization.set()
        results = [first.result(timeout=5)] + [
            future.result(timeout=5) for future in followers
        ]

    assert create_calls == 1
    assert all(client is expected_client for client in results)


def test_r2_upload_marks_audio_assets_as_long_lived_cacheable():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file_with_key(
        BytesIO(b"audio"),
        "static/audio_cache/menu.mp3",
        "audio/mpeg",
    )

    assert url == "https://cdn.chatboc.ar/static/audio_cache/menu.mp3"
    assert client.calls[0]["extra_args"] == {
        "ContentType": "audio/mpeg",
        "CacheControl": "public, max-age=31536000, immutable",
    }


def test_r2_upload_keeps_reclamo_attachments_private_even_when_image():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file_with_key(
        BytesIO(b"photo"),
        "municipios/junin/reclamos/M-378430/foto.jpg",
        "image/jpeg",
    )

    assert url == "https://cdn.chatboc.ar/municipios/junin/reclamos/M-378430/foto.jpg"
    assert client.calls[0]["extra_args"] == {
        "ContentType": "image/jpeg",
        "CacheControl": "private, no-store",
    }


def test_r2_upload_marks_catalog_assets_as_public_with_bounded_cache():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file_with_key(
        BytesIO(b"image"),
        "pymes/bodega/catalogos/malbec-reserva.jpg",
        "image/jpeg",
    )

    assert url == "https://cdn.chatboc.ar/pymes/bodega/catalogos/malbec-reserva.jpg"
    assert client.calls[0]["extra_args"] == {
        "ContentType": "image/jpeg",
        "CacheControl": "public, max-age=604800",
    }


def test_r2_upload_file_generates_public_catalog_key_from_tenant_and_filename():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file(
        BytesIO(b"image"),
        "../Malbec Reserva 2026.JPG",
        "image/jpeg",
        tenant_slug="Bodega Demo",
        context_type="catalogo",
    )

    assert url == "https://cdn.chatboc.ar/pymes/bodega-demo/catalogos/malbec-reserva-2026.jpg"
    assert client.calls[0]["key"] == "pymes/bodega-demo/catalogos/malbec-reserva-2026.jpg"
    assert client.calls[0]["extra_args"]["CacheControl"] == "public, max-age=604800"


def test_r2_upload_file_uses_opaque_names_for_reclamo_attachments():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file(
        BytesIO(b"image"),
        "DNI 32877851 Don Bosco 55.jpg",
        "image/jpeg",
        tenant_slug="Junin",
        context_type="reclamos",
    )

    assert "DNI" not in url
    assert "Don" not in url
    key = client.calls[0]["key"]
    assert re.fullmatch(r"municipios/junin/reclamos/[a-f0-9]{32}\.jpg", key)
    assert client.calls[0]["extra_args"]["CacheControl"] == "private, no-store"


def test_r2_upload_file_uses_opaque_names_for_pedido_attachments():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file(
        BytesIO(b"image"),
        "Pedido DNI 32877851 Don Bosco 55.jpg",
        "image/jpeg",
        tenant_slug="Ferreteria Central",
        context_type="pedidos",
    )

    assert "DNI" not in url
    assert "Don" not in url
    key = client.calls[0]["key"]
    assert re.fullmatch(r"pymes/ferreteria-central/pedidos/[a-f0-9]{32}\.jpg", key)
    assert client.calls[0]["extra_args"]["CacheControl"] == "private, no-store"


def test_upload_to_gcs_uses_r2_service_key_policy_for_public_intake_pedidos(app, monkeypatch):
    client = _RecordingS3Client()
    service = _configured_service(client)
    monkeypatch.setattr(gcs_service, "r2_service", service)

    with app.test_request_context("/api/pedidos/from-file"):
        g.tenant_profile = SimpleNamespace(slug="Ferreteria Central", tipo="pyme")
        file_storage = FileStorage(
            stream=BytesIO(b"pedido manuscrito"),
            filename="Pedido DNI 32877851 Don Bosco 55.jpg",
            content_type="image/jpeg",
        )

        result = gcs_service.upload_to_gcs(file_storage, kind="pedidos")

    assert result is not None
    assert "DNI" not in result["public_url"]
    assert "Don" not in result["public_url"]
    key = client.calls[0]["key"]
    assert re.fullmatch(r"pymes/ferreteria-central/pedidos/[a-f0-9]{32}\.jpg", key)
    assert client.calls[0]["extra_args"]["CacheControl"] == "private, no-store"
    assert result["original_name"] == "Pedido_DNI_32877851_Don_Bosco_55.jpg"


def test_r2_upload_file_generates_static_audio_cache_key_without_tenant_folder():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.upload_file(
        BytesIO(b"audio"),
        "Menu Principal.mp3",
        "audio/mpeg",
        tenant_slug="junin",
        context_type="audio_cache",
    )

    assert url == "https://cdn.chatboc.ar/static/audio_cache/menu-principal.mp3"
    assert client.calls[0]["key"] == "static/audio_cache/menu-principal.mp3"
    assert client.calls[0]["extra_args"]["CacheControl"] == "public, max-age=31536000, immutable"


def test_r2_resolve_download_url_uses_public_cdn_for_public_catalog_asset():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.resolve_download_url("pymes/bodega/catalogos/malbec-reserva.jpg", "image/jpeg")

    assert url == "https://cdn.chatboc.ar/pymes/bodega/catalogos/malbec-reserva.jpg"
    assert client.presigned_calls == []


def test_r2_resolve_download_url_uses_signed_url_for_private_reclamo_asset():
    client = _RecordingS3Client()
    service = _configured_service(client)

    url = service.resolve_download_url(
        "municipios/junin/reclamos/secret.jpg",
        "image/jpeg",
        expires_in=999999,
    )

    assert url == "https://r2-signed.example/municipios/junin/reclamos/secret.jpg?ttl=3600"
    assert client.presigned_calls == [
        {
            "operation": "get_object",
            "params": {"Bucket": "chatboc-assets", "Key": "municipios/junin/reclamos/secret.jpg"},
            "expires_in": 3600,
        }
    ]


def test_r2_extracts_object_key_from_public_cdn_url():
    service = _configured_service(_RecordingS3Client())

    key = service.object_key_from_url("https://cdn.chatboc.ar/municipios/junin/reclamos/foto%201.jpg")

    assert key == "municipios/junin/reclamos/foto 1.jpg"


def test_r2_does_not_treat_local_relative_media_paths_as_object_keys():
    service = _configured_service(_RecordingS3Client())

    assert service.object_key_from_url("/static/uploads/foto.png") is None
    assert service.object_key_from_url("/media/uploads/foto.png") is None


def test_attachment_delivery_keeps_public_catalog_url(monkeypatch):
    client = _RecordingS3Client()
    service = _configured_service(client)
    monkeypatch.setattr(attachment_delivery, "r2_service", service)

    payload = serialize_attachment_for_delivery(
        {
            "id": 10,
            "url": "https://cdn.chatboc.ar/pymes/bodega/catalogos/malbec.jpg",
            "name": "malbec.jpg",
            "mimeType": "image/jpeg",
        }
    )

    assert payload["url"] == "https://cdn.chatboc.ar/pymes/bodega/catalogos/malbec.jpg"
    assert payload["downloadUrl"] == payload["url"]
    assert payload["storage_access"] == "public"
    assert payload["is_private"] is False
    assert client.presigned_calls == []


def test_attachment_delivery_signs_private_reclamo_url(monkeypatch):
    client = _RecordingS3Client()
    service = _configured_service(client)
    monkeypatch.setattr(attachment_delivery, "r2_service", service)

    original_url = "https://cdn.chatboc.ar/municipios/junin/reclamos/secret.jpg"
    payload = serialize_attachment_for_delivery(
        {
            "id": 11,
            "url": original_url,
            "name": "secret.jpg",
            "mimeType": "image/jpeg",
        }
    )

    assert payload["url"] == "https://r2-signed.example/municipios/junin/reclamos/secret.jpg?ttl=900"
    assert payload["downloadUrl"] == payload["url"]
    assert payload["storage_url"] == original_url
    assert payload["storage_access"] == "signed"
    assert payload["is_private"] is True
