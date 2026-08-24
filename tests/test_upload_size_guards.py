from io import BytesIO
from unittest.mock import Mock

import pytest
from flask import Flask, request
from werkzeug.datastructures import FileStorage

from services import gcs_service
from utils.upload_limits import set_request_content_limit, set_upload_request_limit


class RecordingBytesIO(BytesIO):
    def __init__(self, initial_bytes: bytes):
        super().__init__(initial_bytes)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return super().read(size)


def _file_storage(data: bytes, *, content_length: int = 0) -> tuple[FileStorage, RecordingBytesIO]:
    stream = RecordingBytesIO(data)
    return (
        FileStorage(
            stream=stream,
            filename="evidence.png",
            content_type="image/png",
            content_length=content_length,
        ),
        stream,
    )


def test_upload_to_gcs_rejects_at_limit_plus_one_before_any_provider(monkeypatch):
    file_storage, stream = _file_storage(b"12345")
    r2_upload = Mock()
    local_save = Mock()
    cloudinary_save = Mock()
    monkeypatch.setattr(gcs_service.r2_service, "upload_file_with_key", r2_upload)
    monkeypatch.setattr(gcs_service, "_save_to_local", local_save)
    monkeypatch.setattr(gcs_service, "_save_to_cloudinary", cloudinary_save)

    with pytest.raises(gcs_service.UploadFileTooLargeError) as exc_info:
        gcs_service.upload_to_gcs(file_storage, max_file_size=4)

    assert exc_info.value.max_bytes == 4
    assert exc_info.value.observed_bytes == 5
    assert stream.read_sizes == [5]
    r2_upload.assert_not_called()
    local_save.assert_not_called()
    cloudinary_save.assert_not_called()


def test_thumbnail_upload_rejects_before_thumbnail_or_storage(monkeypatch):
    file_storage, stream = _file_storage(b"12345")
    thumbnail = Mock()
    r2_upload = Mock()
    local_save = Mock()
    monkeypatch.setattr(gcs_service, "generar_thumbnail", thumbnail)
    monkeypatch.setattr(gcs_service.r2_service, "upload_file_with_key", r2_upload)
    monkeypatch.setattr(gcs_service, "_save_to_local", local_save)

    with pytest.raises(gcs_service.UploadFileTooLargeError):
        gcs_service.guardar_adjunto_y_thumbnail(file_storage, max_file_size=4)

    assert stream.read_sizes == [5]
    thumbnail.assert_not_called()
    r2_upload.assert_not_called()
    local_save.assert_not_called()


def test_declared_oversize_is_rejected_without_reading_the_stream():
    file_storage, stream = _file_storage(b"12345", content_length=5)

    with pytest.raises(gcs_service.UploadFileTooLargeError) as exc_info:
        gcs_service.validate_upload_size(file_storage, max_bytes=4)

    assert exc_info.value.observed_bytes == 5
    assert stream.read_sizes == []


def test_exact_limit_is_valid_and_rewinds_for_the_uploader():
    file_storage, stream = _file_storage(b"1234")

    assert gcs_service.validate_upload_size(file_storage, max_bytes=4) == 4
    assert stream.read_sizes == [5]
    assert stream.tell() == 0


def test_request_limit_uses_file_count_and_multipart_overhead():
    app = Flask(__name__)

    with app.test_request_context("/upload", method="POST"):
        applied = set_upload_request_limit(
            10,
            max_files=3,
            multipart_overhead_bytes=7,
        )

        assert applied == 37
        assert request.max_content_length == 37


def test_request_limit_never_relaxes_a_stricter_application_cap():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 23

    with app.test_request_context("/upload", method="POST"):
        applied = set_upload_request_limit(
            10,
            max_files=3,
            multipart_overhead_bytes=7,
        )

        assert applied == 23
        assert request.max_content_length == 23


def test_plain_request_limit_applies_without_multipart_overhead():
    app = Flask(__name__)

    with app.test_request_context("/json", method="POST"):
        applied = set_request_content_limit(64)

        assert applied == 64
        assert request.max_content_length == 64
