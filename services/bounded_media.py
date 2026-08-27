"""Bounded streaming helpers for provider-hosted media downloads."""

from __future__ import annotations

from typing import Any

import requests

from services.outbox_execution_budget import require_outbox_time_remaining


class MediaDownloadTooLarge(ValueError):
    """The response body exceeds the configured media limit."""


def read_bounded_response_body(
    response: Any,
    *,
    max_bytes: int,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read an HTTP response incrementally and fail before buffering too much."""

    resolved_limit = max(1, int(max_bytes))
    headers = getattr(response, "headers", None)
    raw_content_length = headers.get("Content-Length") if headers is not None else None
    if isinstance(raw_content_length, (str, int)):
        try:
            declared_size = int(raw_content_length)
        except (TypeError, ValueError, OverflowError):
            declared_size = None
        if declared_size is not None and declared_size > resolved_limit:
            raise MediaDownloadTooLarge("declared media size exceeds limit")

    chunks: list[bytes] = []
    downloaded = 0
    for chunk in response.iter_content(chunk_size=max(1, int(chunk_size))):
        require_outbox_time_remaining()
        if not chunk:
            continue
        downloaded += len(chunk)
        if downloaded > resolved_limit:
            raise MediaDownloadTooLarge("streamed media size exceeds limit")
        chunks.append(bytes(chunk))

    # Old test doubles expose only ``content``. Real requests responses always
    # stay on the streamed path so this compatibility seam cannot buffer an
    # untrusted production body without the limit being enforced first.
    if not chunks and not isinstance(response, requests.Response):
        mock_content = getattr(response, "content", b"")
        if isinstance(mock_content, (bytes, bytearray)):
            if len(mock_content) > resolved_limit:
                raise MediaDownloadTooLarge("media size exceeds limit")
            return bytes(mock_content)

    return b"".join(chunks)


__all__ = ["MediaDownloadTooLarge", "read_bounded_response_body"]
