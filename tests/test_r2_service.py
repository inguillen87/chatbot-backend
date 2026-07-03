from io import BytesIO
from unittest.mock import patch

from services.r2_service import R2Service


class _RecordingS3Client:
    def __init__(self):
        self.calls = []

    def upload_fileobj(self, file_obj, bucket_name, key, ExtraArgs=None):
        self.calls.append(
            {
                "file_obj": file_obj,
                "bucket_name": bucket_name,
                "key": key,
                "extra_args": dict(ExtraArgs or {}),
            }
        )


def _configured_service(client):
    with patch.dict("os.environ", {}, clear=True):
        service = R2Service()
    service.client = client
    service.bucket_name = "chatboc-assets"
    service.public_base_url = "https://cdn.chatboc.ar"
    return service


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
        "CacheControl": "public, max-age=31536000",
    }
