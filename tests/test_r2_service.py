from io import BytesIO
import re
from types import SimpleNamespace
from unittest.mock import patch

from botocore.exceptions import ClientError
from flask import g
import pytest
from werkzeug.datastructures import FileStorage

import services.gcs_service as gcs_service
from services.r2_service import (
    R2ObjectStorageUnavailableError,
    R2Service,
    R2SourceObjectChangedError,
)
from services.attachment_delivery import serialize_attachment_for_delivery
import services.attachment_delivery as attachment_delivery


class _RecordingS3Client:
    def __init__(self):
        self.calls = []
        self.presigned_calls = []
        self.head_calls = []
        self.copy_calls = []
        self.delete_calls = []

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

    def head_object(self, **kwargs):
        self.head_calls.append(dict(kwargs))
        return {
            "ContentLength": 4,
            "ContentType": "image/png",
            "ETag": '"source-etag"',
        }

    def copy_object(self, **kwargs):
        self.copy_calls.append(dict(kwargs))
        return {"CopyObjectResult": {"ETag": "etag"}}

    def delete_object(self, **kwargs):
        self.delete_calls.append(dict(kwargs))
        return {}


def _configured_service(client):
    with patch.dict("os.environ", {}, clear=True):
        service = R2Service()
    service.client = client
    service.bucket_name = "chatboc-assets"
    service.public_base_url = "https://cdn.chatboc.ar"
    return service


def _client_error(code, status_code):
    return ClientError(
        {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        },
        "R2ObjectOperation",
    )


def test_configured_service_builds_r2_client_only_on_first_operation():
    client = _RecordingS3Client()
    environment = {
        "R2_ENDPOINT_URL": "https://r2.example.test",
        "R2_ACCESS_KEY_ID": "test-access-key",
        "R2_SECRET_ACCESS_KEY": "test-secret-key",
        "R2_BUCKET_NAME": "chatboc-assets",
    }

    with patch.dict("os.environ", environment, clear=True):
        service = R2Service()

    assert service.client is None
    with patch.object(service, "_create_client", return_value=client) as create_client:
        assert service.is_configured is True
        assert service.is_configured is True

    create_client.assert_called_once_with()
    assert service.client is client


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


def test_r2_upload_file_uses_one_client_lookup_per_upload():
    client = _RecordingS3Client()
    service = _configured_service(client)

    with patch.object(service, "_get_client", wraps=service._get_client) as get_client:
        url = service.upload_file(
            BytesIO(b"image"),
            "luminaria.jpg",
            "image/jpeg",
            tenant_slug="junin",
            context_type="reclamos",
        )

    assert url is not None
    assert get_client.call_count == 1


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


def test_r2_direct_upload_operations_bind_mime_and_keep_final_attachment_private():
    client = _RecordingS3Client()
    service = _configured_service(client)
    temporary_key = "uploads/junin/chat-attachments/upload.png"
    final_key = "general/attachments/junin/final.png"

    upload_url = service.generate_presigned_upload_url(
        temporary_key,
        "image/png",
        4,
        expires_in=999999,
    )
    metadata = service.head_object(temporary_key)
    copied = service.copy_object(
        temporary_key,
        final_key,
        "image/png",
        source_etag='"source-etag"',
    )
    deleted = service.delete_object(temporary_key)

    assert upload_url.endswith("?ttl=900")
    assert client.presigned_calls[-1] == {
        "operation": "put_object",
        "params": {
            "Bucket": "chatboc-assets",
            "Key": temporary_key,
            "ContentType": "image/png",
            "ContentLength": 4,
        },
        "expires_in": 900,
    }
    assert metadata == {
        "ContentLength": 4,
        "ContentType": "image/png",
        "ETag": '"source-etag"',
    }
    assert copied is True
    assert client.copy_calls == [
        {
            "Bucket": "chatboc-assets",
            "Key": final_key,
            "CopySource": {
                "Bucket": "chatboc-assets",
                "Key": temporary_key,
            },
            "CopySourceIfMatch": '"source-etag"',
            "MetadataDirective": "REPLACE",
            "ContentType": "image/png",
            "CacheControl": "private, no-store",
        }
    ]
    assert deleted is True
    assert client.delete_calls == [
        {"Bucket": "chatboc-assets", "Key": temporary_key}
    ]


def test_r2_head_returns_none_only_for_definite_object_not_found():
    client = _RecordingS3Client()
    service = _configured_service(client)

    with patch.object(
        client,
        "head_object",
        side_effect=_client_error("NoSuchKey", 404),
    ):
        assert service.head_object("uploads/junin/missing.png") is None


@pytest.mark.parametrize(
    ("code", "status_code"),
    (("AccessDenied", 403), ("SlowDown", 503), ("Unknown", 0)),
)
def test_r2_head_raises_typed_unavailability_for_non_definitive_errors(
    code,
    status_code,
):
    client = _RecordingS3Client()
    service = _configured_service(client)

    with patch.object(
        client,
        "head_object",
        side_effect=_client_error(code, status_code),
    ), pytest.raises(R2ObjectStorageUnavailableError):
        service.head_object("uploads/junin/evidence.png")


def test_r2_copy_distinguishes_transient_failure_from_etag_race():
    client = _RecordingS3Client()
    service = _configured_service(client)

    with patch.object(
        client,
        "copy_object",
        side_effect=_client_error("ServiceUnavailable", 503),
    ), pytest.raises(R2ObjectStorageUnavailableError):
        service.copy_object(
            "uploads/junin/source.png",
            "general/attachments/junin/final.png",
            "image/png",
            source_etag='"source-etag"',
        )

    with patch.object(
        client,
        "copy_object",
        side_effect=_client_error("PreconditionFailed", 412),
    ), pytest.raises(R2SourceObjectChangedError):
        service.copy_object(
            "uploads/junin/source.png",
            "general/attachments/junin/final.png",
            "image/png",
            source_etag='"source-etag"',
        )


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
