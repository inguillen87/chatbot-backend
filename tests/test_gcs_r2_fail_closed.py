import io

import pytest
from flask import Flask
from werkzeug.datastructures import FileStorage

from services import gcs_service


@pytest.fixture
def app():
    return Flask(__name__)


def _file(name: str = "evidence.txt") -> FileStorage:
    return FileStorage(
        stream=io.BytesIO(b"durable evidence"),
        filename=name,
        content_type="text/plain",
    )


def _local_result(original_filename, file_bytes, unique_name, mimetype, *_args, **_kwargs):
    return {
        "unique_name": unique_name,
        "original_url": f"https://fallback.example/{unique_name}",
        "size": len(file_bytes),
        "original_name": original_filename,
        "mimetype": mimetype,
        "thumb_meta": None,
        "thumbUrl": None,
    }


def _raise_r2_unavailable(*_args, **_kwargs):
    raise RuntimeError("R2 unavailable")


@pytest.mark.parametrize(
    ("entrypoint", "expected_url_key"),
    [
        (gcs_service.upload_to_gcs, "public_url"),
        (gcs_service.guardar_adjunto_y_thumbnail, "original_url"),
    ],
)
def test_r2_gate_defaults_off_and_preserves_fallback(
    app,
    monkeypatch,
    entrypoint,
    expected_url_key,
):
    monkeypatch.delenv("VERCEL_DURABLE_UPLOADS_REQUIRE_R2", raising=False)
    monkeypatch.setattr(gcs_service.r2_service, "upload_file_with_key", lambda *_a, **_k: None)
    monkeypatch.setattr(gcs_service, "CLOUDINARY_ENABLED", False)
    monkeypatch.setattr(gcs_service, "GCS_ENABLED", False)
    monkeypatch.setattr(gcs_service, "VERCEL_BLOB_RW_TOKEN", None)
    monkeypatch.setattr(gcs_service, "_save_to_local", _local_result)
    monkeypatch.setattr(gcs_service, "generar_thumbnail", lambda *_a, **_k: (None, None))

    with app.test_request_context("/api/uploads"):
        result = entrypoint(_file())

    assert result is not None
    assert result[expected_url_key].startswith("https://fallback.example/")


@pytest.mark.parametrize(
    "entrypoint",
    [gcs_service.upload_to_gcs, gcs_service.guardar_adjunto_y_thumbnail],
)
def test_r2_gate_fails_closed_without_secondary_fallback(
    app,
    monkeypatch,
    entrypoint,
):
    monkeypatch.setenv("VERCEL_DURABLE_UPLOADS_REQUIRE_R2", "true")
    monkeypatch.setattr(gcs_service.r2_service, "upload_file_with_key", lambda *_a, **_k: None)
    monkeypatch.setattr(gcs_service, "generar_thumbnail", lambda *_a, **_k: (None, None))

    fallback_calls = []
    monkeypatch.setattr(
        gcs_service,
        "_ensure_cloudinary_initialized",
        lambda *_a, **_k: fallback_calls.append("cloudinary"),
    )
    monkeypatch.setattr(
        gcs_service,
        "_save_to_local",
        lambda *_a, **_k: fallback_calls.append("local"),
    )
    monkeypatch.setattr(
        gcs_service,
        "_get_gcs_client",
        lambda: fallback_calls.append("gcs"),
    )

    with app.test_request_context("/api/uploads"):
        result = entrypoint(_file())

    assert result is None
    assert fallback_calls == []


@pytest.mark.parametrize(
    "entrypoint",
    [gcs_service.upload_to_gcs, gcs_service.guardar_adjunto_y_thumbnail],
)
def test_r2_gate_fails_closed_when_r2_raises(
    app,
    monkeypatch,
    entrypoint,
):
    monkeypatch.setenv("VERCEL_DURABLE_UPLOADS_REQUIRE_R2", "enabled")
    monkeypatch.setattr(
        gcs_service.r2_service,
        "upload_file_with_key",
        _raise_r2_unavailable,
    )
    monkeypatch.setattr(gcs_service, "generar_thumbnail", lambda *_a, **_k: (None, None))

    fallback_calls = []
    monkeypatch.setattr(
        gcs_service,
        "_ensure_cloudinary_initialized",
        lambda *_a, **_k: fallback_calls.append("cloudinary"),
    )
    monkeypatch.setattr(
        gcs_service,
        "_save_to_local",
        lambda *_a, **_k: fallback_calls.append("local"),
    )

    with app.test_request_context("/api/uploads"):
        result = entrypoint(_file())

    assert result is None
    assert fallback_calls == []


def test_r2_gate_fails_closed_and_cleans_up_when_thumbnail_upload_fails(
    app,
    monkeypatch,
):
    monkeypatch.setenv("VERCEL_DURABLE_UPLOADS_REQUIRE_R2", "true")
    upload_results = iter(["https://cdn.example/original", None])
    monkeypatch.setattr(
        gcs_service.r2_service,
        "upload_file_with_key",
        lambda *_a, **_k: next(upload_results),
    )
    monkeypatch.setattr(
        gcs_service,
        "generar_thumbnail",
        lambda *_a, **_k: (b"thumbnail", {"width": 1}),
    )
    deleted_keys = []
    monkeypatch.setattr(
        gcs_service.r2_service,
        "delete_object",
        lambda key: deleted_keys.append(key),
    )
    fallback_calls = []
    monkeypatch.setattr(
        gcs_service,
        "_ensure_cloudinary_initialized",
        lambda *_a, **_k: fallback_calls.append("cloudinary"),
    )

    with app.test_request_context("/api/uploads"):
        result = gcs_service.guardar_adjunto_y_thumbnail(_file())

    assert result is None
    assert len(deleted_keys) == 2
    assert fallback_calls == []


@pytest.mark.parametrize(
    ("entrypoint", "expected_url_key"),
    [
        (gcs_service.upload_to_gcs, "public_url"),
        (gcs_service.guardar_adjunto_y_thumbnail, "original_url"),
    ],
)
def test_r2_gate_returns_successful_r2_upload(
    app,
    monkeypatch,
    entrypoint,
    expected_url_key,
):
    monkeypatch.setenv("VERCEL_DURABLE_UPLOADS_REQUIRE_R2", "1")
    monkeypatch.setattr(
        gcs_service.r2_service,
        "upload_file_with_key",
        lambda _stream, key, _content_type: f"https://cdn.example/{key}",
    )
    monkeypatch.setattr(gcs_service, "generar_thumbnail", lambda *_a, **_k: (None, None))

    with app.test_request_context("/api/uploads"):
        result = entrypoint(_file())

    assert result is not None
    assert result[expected_url_key].startswith("https://cdn.example/")
