"""Secure download helpers for media returned by WhatsApp Flow responses.

PhotoPicker and DocumentPicker values received in the terminal Flow response
contain Meta media identifiers. The identifiers are resolved through the Cloud
API, downloaded with the tenant-scoped access token, and validated before any
storage write is attempted.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urljoin, urlsplit

import requests
from werkzeug.utils import secure_filename

from services.meta_flow_management import resolve_meta_graph_credentials


MAX_PHOTOS = 3
MAX_DOCUMENTS = 3
MAX_TOTAL_FILES = 5
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024

PHOTO_MIME_TYPES = frozenset({"image/jpeg", "image/png"})
DOCUMENT_MIME_TYPES = frozenset(
    {
        "application/msword",
        "application/pdf",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.oasis.opendocument.presentation",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
    }
)

_MEDIA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SAFE_MEDIA_HOST_SUFFIXES = (
    ".facebook.com",
    ".fbcdn.net",
    ".fbsbx.com",
    ".whatsapp.net",
)


class MetaFlowMediaError(RuntimeError):
    """A bounded error whose message never contains provider payload values."""

    def __init__(self, code: str, *, status_code: int = 400) -> None:
        self.code = str(code or "flow_media_invalid")[:80]
        self.status_code = int(status_code)
        super().__init__(self.code)


@dataclass(frozen=True)
class FlowMediaDescriptor:
    kind: str
    media_id: str
    file_name: str
    mime_type: str
    file_size: int | None
    sha256: bytes | None

    @property
    def max_bytes(self) -> int:
        return MAX_PHOTO_BYTES if self.kind == "photo" else MAX_DOCUMENT_BYTES


@dataclass(frozen=True)
class DownloadedFlowMedia:
    kind: str
    media_id: str
    file_name: str
    mime_type: str
    content: bytes
    sha256_hex: str

    @property
    def size(self) -> int:
        return len(self.content)


def normalize_claim_evidence(
    photos: Any,
    documents: Any,
    *,
    require_evidence: bool = True,
) -> tuple[FlowMediaDescriptor, ...]:
    """Return a strict, deduplicated descriptor list from a Flow response."""

    normalized = [
        *_normalize_collection(
            photos,
            kind="photo",
            max_items=MAX_PHOTOS,
            allowed_mime_types=PHOTO_MIME_TYPES,
            max_bytes=MAX_PHOTO_BYTES,
        ),
        *_normalize_collection(
            documents,
            kind="document",
            max_items=MAX_DOCUMENTS,
            allowed_mime_types=DOCUMENT_MIME_TYPES,
            max_bytes=MAX_DOCUMENT_BYTES,
        ),
    ]
    if require_evidence and not normalized:
        raise MetaFlowMediaError("claim_evidence_required")
    if len(normalized) > MAX_TOTAL_FILES:
        raise MetaFlowMediaError("claim_evidence_too_many_files")

    known_total = sum(item.file_size or 0 for item in normalized)
    if known_total > MAX_TOTAL_BYTES:
        raise MetaFlowMediaError("claim_evidence_too_large")
    media_ids = [item.media_id for item in normalized]
    if len(media_ids) != len(set(media_ids)):
        raise MetaFlowMediaError("claim_evidence_duplicate_media")
    return tuple(normalized)


def download_claim_evidence_media(
    *,
    provider_sender: Any,
    descriptors: Sequence[FlowMediaDescriptor],
    app_config: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
    http: Any = requests,
) -> tuple[DownloadedFlowMedia, ...]:
    """Download every descriptor before callers perform a storage write."""

    waba_id = str(getattr(provider_sender, "waba_id", None) or "").strip()
    phone_number_id = str(
        getattr(provider_sender, "phone_number_id", None) or ""
    ).strip()
    if not phone_number_id or not _MEDIA_ID.fullmatch(phone_number_id):
        raise MetaFlowMediaError("meta_phone_number_id_not_configured", status_code=503)

    credentials = resolve_meta_graph_credentials(
        waba_id=waba_id,
        app_config=app_config,
        environ=environ,
    )
    if not credentials.ready or not credentials.access_token:
        raise MetaFlowMediaError("meta_graph_media_not_ready", status_code=503)

    client = MetaFlowMediaClient(credentials=credentials, http=http)
    downloaded: list[DownloadedFlowMedia] = []
    aggregate_size = 0
    for descriptor in descriptors:
        item = client.download(descriptor, phone_number_id=phone_number_id)
        aggregate_size += item.size
        if aggregate_size > MAX_TOTAL_BYTES:
            raise MetaFlowMediaError("claim_evidence_too_large")
        downloaded.append(item)
    return tuple(downloaded)


class MetaFlowMediaClient:
    def __init__(self, *, credentials: Any, http: Any = requests) -> None:
        self.credentials = credentials
        self._http = http

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.credentials.access_token}",
            "Accept": "application/json",
        }

    def download(
        self,
        descriptor: FlowMediaDescriptor,
        *,
        phone_number_id: str,
    ) -> DownloadedFlowMedia:
        metadata_url = (
            f"{self.credentials.graph_base_url}/{descriptor.media_id}"
        )
        try:
            response = self._http.get(
                metadata_url,
                headers=self._headers,
                params={"phone_number_id": phone_number_id},
                timeout=self.credentials.timeout_seconds,
            )
        except Exception as exc:
            raise MetaFlowMediaError("meta_media_lookup_failed", status_code=502) from exc
        metadata = _response_json(response, code="meta_media_lookup_invalid")
        if not 200 <= int(getattr(response, "status_code", 0) or 0) < 300:
            raise MetaFlowMediaError("meta_media_lookup_failed", status_code=502)

        returned_id = str(metadata.get("id") or descriptor.media_id).strip()
        if returned_id != descriptor.media_id:
            raise MetaFlowMediaError("meta_media_scope_mismatch", status_code=403)
        media_url = str(metadata.get("url") or "").strip()
        if not _safe_media_url(media_url):
            raise MetaFlowMediaError("meta_media_url_invalid", status_code=502)

        resolved_mime = str(metadata.get("mime_type") or descriptor.mime_type).strip().lower()
        if resolved_mime != descriptor.mime_type:
            raise MetaFlowMediaError("claim_evidence_mime_mismatch")
        allowed = PHOTO_MIME_TYPES if descriptor.kind == "photo" else DOCUMENT_MIME_TYPES
        if resolved_mime not in allowed:
            raise MetaFlowMediaError("claim_evidence_mime_invalid")

        metadata_size = _optional_positive_int(metadata.get("file_size"))
        if metadata_size and metadata_size > descriptor.max_bytes:
            raise MetaFlowMediaError("claim_evidence_too_large")
        if descriptor.file_size and metadata_size and descriptor.file_size != metadata_size:
            raise MetaFlowMediaError("claim_evidence_size_mismatch")

        metadata_digest = _decode_sha256(metadata.get("sha256"), required=True)
        if descriptor.sha256 and not _constant_digest_equal(
            descriptor.sha256,
            metadata_digest,
        ):
            raise MetaFlowMediaError("claim_evidence_hash_mismatch")

        content, response_mime = self._download_url(
            media_url,
            max_bytes=descriptor.max_bytes,
        )
        if response_mime and response_mime != resolved_mime:
            raise MetaFlowMediaError("claim_evidence_mime_mismatch")
        if metadata_size and len(content) != metadata_size:
            raise MetaFlowMediaError("claim_evidence_size_mismatch")

        digest = hashlib.sha256(content).digest()
        if not _constant_digest_equal(digest, metadata_digest):
            raise MetaFlowMediaError("claim_evidence_hash_mismatch")
        _validate_file_signature(content, resolved_mime)
        return DownloadedFlowMedia(
            kind=descriptor.kind,
            media_id=descriptor.media_id,
            file_name=descriptor.file_name,
            mime_type=resolved_mime,
            content=content,
            sha256_hex=digest.hex(),
        )

    def _download_url(self, url: str, *, max_bytes: int) -> tuple[bytes, str | None]:
        current_url = url
        for redirect_count in range(3):
            try:
                response = self._http.get(
                    current_url,
                    headers={
                        "Authorization": f"Bearer {self.credentials.access_token}",
                        "Accept": "*/*",
                    },
                    timeout=self.credentials.timeout_seconds,
                    allow_redirects=False,
                    stream=True,
                )
            except Exception as exc:
                raise MetaFlowMediaError("meta_media_download_failed", status_code=502) from exc

            status = int(getattr(response, "status_code", 0) or 0)
            if status in {301, 302, 303, 307, 308}:
                if redirect_count >= 2:
                    raise MetaFlowMediaError("meta_media_redirect_invalid", status_code=502)
                location = str((getattr(response, "headers", {}) or {}).get("Location") or "")
                candidate = urljoin(current_url, location)
                if not _safe_media_url(candidate):
                    raise MetaFlowMediaError("meta_media_redirect_invalid", status_code=502)
                current_url = candidate
                continue
            if not 200 <= status < 300:
                raise MetaFlowMediaError("meta_media_download_failed", status_code=502)

            headers = getattr(response, "headers", {}) or {}
            content_length = _optional_positive_int(headers.get("Content-Length"))
            if content_length and content_length > max_bytes:
                raise MetaFlowMediaError("claim_evidence_too_large")
            content = _bounded_response_bytes(response, max_bytes=max_bytes)
            content_type = str(headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return content, content_type or None
        raise MetaFlowMediaError("meta_media_download_failed", status_code=502)


def _normalize_collection(
    value: Any,
    *,
    kind: str,
    max_items: int,
    allowed_mime_types: Iterable[str],
    max_bytes: int,
) -> list[FlowMediaDescriptor]:
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise MetaFlowMediaError("claim_evidence_metadata_invalid")
    allowed = set(allowed_mime_types)
    items: list[FlowMediaDescriptor] = []
    for raw in value:
        if not isinstance(raw, Mapping):
            raise MetaFlowMediaError("claim_evidence_metadata_invalid")
        media_id = str(raw.get("id") or raw.get("media_id") or "").strip()
        if not _MEDIA_ID.fullmatch(media_id):
            raise MetaFlowMediaError("claim_evidence_metadata_invalid")
        mime_type = str(
            raw.get("mime_type") or raw.get("mimetype") or raw.get("mime") or ""
        ).strip().lower()
        if mime_type not in allowed:
            raise MetaFlowMediaError("claim_evidence_metadata_invalid")
        file_name = secure_filename(
            str(raw.get("file_name") or raw.get("filename") or raw.get("name") or "")
        )[:180]
        if not file_name:
            file_name = f"evidencia-{media_id[:12]}{_extension_for_mime(mime_type)}"
        file_size = _optional_positive_int(raw.get("file_size") or raw.get("size"))
        if file_size and file_size > max_bytes:
            raise MetaFlowMediaError("claim_evidence_metadata_invalid")
        sha256 = _decode_sha256(raw.get("sha256"), required=False)
        items.append(
            FlowMediaDescriptor(
                kind=kind,
                media_id=media_id,
                file_name=file_name,
                mime_type=mime_type,
                file_size=file_size,
                sha256=sha256,
            )
        )
    return items


def _response_json(response: Any, *, code: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as exc:
        raise MetaFlowMediaError(code, status_code=502) from exc
    if not isinstance(payload, dict):
        raise MetaFlowMediaError(code, status_code=502)
    return payload


def _bounded_response_bytes(response: Any, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        source = iterator(chunk_size=64 * 1024)
    else:
        source = (getattr(response, "content", b""),)
    for chunk in source:
        if not chunk:
            continue
        if not isinstance(chunk, (bytes, bytearray)):
            raise MetaFlowMediaError("meta_media_download_invalid", status_code=502)
        size += len(chunk)
        if size > max_bytes:
            raise MetaFlowMediaError("claim_evidence_too_large")
        chunks.append(bytes(chunk))
    content = b"".join(chunks)
    if not content:
        raise MetaFlowMediaError("meta_media_download_empty", status_code=502)
    return content


def _decode_sha256(value: Any, *, required: bool) -> bytes | None:
    text = str(value or "").strip()
    if not text:
        if required:
            raise MetaFlowMediaError("meta_media_hash_missing", status_code=502)
        return None
    if re.fullmatch(r"[0-9a-fA-F]{64}", text):
        return bytes.fromhex(text)
    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MetaFlowMediaError("claim_evidence_hash_invalid") from exc
    if len(decoded) != hashlib.sha256().digest_size:
        raise MetaFlowMediaError("claim_evidence_hash_invalid")
    return decoded


def _constant_digest_equal(left: bytes, right: bytes) -> bool:
    import hmac

    return hmac.compare_digest(left, right)


def _optional_positive_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MetaFlowMediaError("claim_evidence_metadata_invalid")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise MetaFlowMediaError("claim_evidence_metadata_invalid") from exc
    if normalized <= 0:
        raise MetaFlowMediaError("claim_evidence_metadata_invalid")
    return normalized


def _safe_media_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or ""))
        port = parsed.port
    except ValueError:
        return False
    host = str(parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme.lower() != "https" or not host or parsed.username or parsed.password:
        return False
    if port not in (None, 443):
        return False
    return any(host.endswith(suffix) for suffix in _SAFE_MEDIA_HOST_SUFFIXES)


def _validate_file_signature(content: bytes, mime_type: str) -> None:
    valid = False
    if mime_type == "image/jpeg":
        valid = content.startswith(b"\xff\xd8\xff")
    elif mime_type == "image/png":
        valid = content.startswith(b"\x89PNG\r\n\x1a\n")
    elif mime_type == "application/pdf":
        valid = content.startswith(b"%PDF-")
    elif mime_type == "text/plain":
        try:
            valid = b"\x00" not in content and bool(content.decode("utf-8"))
        except UnicodeDecodeError:
            valid = False
    elif mime_type.startswith("application/vnd.openxmlformats-") or mime_type.startswith(
        "application/vnd.oasis.opendocument."
    ):
        valid = content.startswith(b"PK\x03\x04")
    elif mime_type in {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }:
        valid = content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if not valid:
        raise MetaFlowMediaError("claim_evidence_content_invalid")


def _extension_for_mime(mime_type: str) -> str:
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }.get(mime_type, ".bin")


__all__ = [
    "DOCUMENT_MIME_TYPES",
    "DownloadedFlowMedia",
    "FlowMediaDescriptor",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENTS",
    "MAX_PHOTO_BYTES",
    "MAX_PHOTOS",
    "MAX_TOTAL_BYTES",
    "MAX_TOTAL_FILES",
    "MetaFlowMediaClient",
    "MetaFlowMediaError",
    "PHOTO_MIME_TYPES",
    "download_claim_evidence_media",
    "normalize_claim_evidence",
]
