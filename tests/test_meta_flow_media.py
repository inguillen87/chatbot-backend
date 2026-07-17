from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.meta_flow_media import (
    FlowMediaDescriptor,
    MetaFlowMediaClient,
    MetaFlowMediaError,
    normalize_claim_evidence,
)


class _Response:
    def __init__(self, *, status=200, payload=None, headers=None, chunks=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self._chunks = chunks or []

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=64 * 1024):
        del chunk_size
        yield from self._chunks


class _Http:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _client(http):
    return MetaFlowMediaClient(
        credentials=SimpleNamespace(
            graph_base_url="https://graph.facebook.com/v23.0",
            access_token="tenant-scoped-token",
            timeout_seconds=8.0,
        ),
        http=http,
    )


def test_normalize_claim_evidence_ignores_client_urls_and_deduplicates_media():
    descriptors = normalize_claim_evidence(
        [
            {
                "id": "media-photo-1",
                "file_name": "evidencia.jpg",
                "mime_type": "image/jpeg",
                "file_size": 1024,
                "sha256": "a" * 64,
                "cdn_url": "https://attacker.example/evidencia.jpg",
            }
        ],
        [],
    )

    assert len(descriptors) == 1
    assert descriptors[0].media_id == "media-photo-1"
    assert not hasattr(descriptors[0], "cdn_url")

    with pytest.raises(MetaFlowMediaError) as error:
        normalize_claim_evidence(
            [
                {"id": "same-media", "file_name": "a.jpg", "mime_type": "image/jpeg"},
                {"id": "same-media", "file_name": "b.jpg", "mime_type": "image/jpeg"},
            ],
            [],
        )
    assert error.value.code == "claim_evidence_duplicate_media"


def test_meta_media_client_downloads_tenant_scoped_file_and_checks_digest():
    content = b"\xff\xd8\xffverified-photo"
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    http = _Http(
        _Response(
            payload={
                "id": "media-photo-1",
                "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/file",
                "mime_type": "image/jpeg",
                "file_size": len(content),
                "sha256": digest,
            }
        ),
        _Response(
            headers={"Content-Type": "image/jpeg", "Content-Length": str(len(content))},
            chunks=[content[:5], content[5:]],
        ),
    )
    descriptor = FlowMediaDescriptor(
        kind="photo",
        media_id="media-photo-1",
        file_name="evidencia.jpg",
        mime_type="image/jpeg",
        file_size=len(content),
        sha256=bytes.fromhex(digest),
    )

    result = _client(http).download(descriptor, phone_number_id="phone-number-1")

    assert result.content == content
    assert result.sha256_hex == digest
    assert http.calls[0][1]["params"] == {"phone_number_id": "phone-number-1"}
    assert http.calls[1][1]["allow_redirects"] is False
    assert http.calls[1][1]["headers"]["Authorization"] == "Bearer tenant-scoped-token"


@pytest.mark.parametrize(
    "media_url",
    [
        "http://lookaside.fbsbx.com/file",
        "https://attacker.example/file",
        "https://lookaside.fbsbx.com:bad/file",
    ],
)
def test_meta_media_client_rejects_untrusted_or_malformed_provider_urls(media_url):
    http = _Http(
        _Response(
            payload={
                "id": "media-photo-1",
                "url": media_url,
                "mime_type": "image/jpeg",
                "file_size": 3,
                "sha256": "a" * 64,
            }
        )
    )
    descriptor = FlowMediaDescriptor(
        kind="photo",
        media_id="media-photo-1",
        file_name="evidencia.jpg",
        mime_type="image/jpeg",
        file_size=3,
        sha256=bytes.fromhex("a" * 64),
    )

    with pytest.raises(MetaFlowMediaError) as error:
        _client(http).download(descriptor, phone_number_id="phone-number-1")
    assert error.value.code == "meta_media_url_invalid"


def test_meta_media_client_blocks_redirect_outside_meta_hosts():
    content = b"\xff\xd8\xffverified-photo"
    import hashlib

    digest = hashlib.sha256(content).hexdigest()
    http = _Http(
        _Response(
            payload={
                "id": "media-photo-1",
                "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/file",
                "mime_type": "image/jpeg",
                "file_size": len(content),
                "sha256": digest,
            }
        ),
        _Response(status=302, headers={"Location": "https://attacker.example/file"}),
    )
    descriptor = FlowMediaDescriptor(
        kind="photo",
        media_id="media-photo-1",
        file_name="evidencia.jpg",
        mime_type="image/jpeg",
        file_size=len(content),
        sha256=bytes.fromhex(digest),
    )

    with pytest.raises(MetaFlowMediaError) as error:
        _client(http).download(descriptor, phone_number_id="phone-number-1")
    assert error.value.code == "meta_media_redirect_invalid"
