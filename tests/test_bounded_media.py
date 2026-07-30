import pytest

from services.bounded_media import MediaDownloadTooLarge, read_bounded_response_body


class _StreamResponse:
    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = dict(headers or {})

    def iter_content(self, *, chunk_size):
        assert chunk_size > 0
        yield from self._chunks


def test_bounded_media_rejects_declared_oversize_before_streaming():
    response = _StreamResponse(
        [b"not-read"],
        headers={"Content-Length": "5"},
    )

    with pytest.raises(MediaDownloadTooLarge):
        read_bounded_response_body(response, max_bytes=4)


def test_bounded_media_rejects_chunked_oversize_without_content_length():
    response = _StreamResponse([b"123", b"45"])

    with pytest.raises(MediaDownloadTooLarge):
        read_bounded_response_body(response, max_bytes=4)


def test_bounded_media_returns_exact_streamed_bytes_within_limit():
    response = _StreamResponse([b"12", b"", b"34"])

    assert read_bounded_response_body(response, max_bytes=4) == b"1234"
