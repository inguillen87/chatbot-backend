from flask import request


DEFAULT_MULTIPART_OVERHEAD_BYTES = 1024 * 1024


class UploadFileTooLargeError(ValueError):
    """Raised before storage side effects when an upload exceeds its byte cap.

    This exception lives outside the provider module so its identity remains stable
    when storage configuration tests reload ``services.gcs_service``.
    """

    def __init__(
        self,
        *,
        filename: str,
        max_bytes: int,
        observed_bytes: int | None = None,
    ):
        super().__init__(f"Upload exceeds the configured limit of {max_bytes} bytes")
        self.filename = filename
        self.max_bytes = max_bytes
        self.observed_bytes = observed_bytes


def set_request_content_limit(max_request_bytes: int) -> int:
    """Apply a request cap without relaxing a stricter application limit."""

    proposed_limit = int(max_request_bytes)
    if proposed_limit <= 0:
        raise ValueError("request content limit must be positive")

    configured_limit = request.max_content_length
    request.max_content_length = (
        proposed_limit
        if configured_limit is None
        else min(int(configured_limit), proposed_limit)
    )
    return int(request.max_content_length)


def set_upload_request_limit(
    max_file_bytes: int,
    *,
    max_files: int = 1,
    multipart_overhead_bytes: int = DEFAULT_MULTIPART_OVERHEAD_BYTES,
) -> int:
    """Bound request parsing without relaxing a stricter application limit."""

    file_limit = int(max_file_bytes)
    file_count = int(max_files)
    overhead = int(multipart_overhead_bytes)
    if file_limit <= 0 or file_count <= 0 or overhead < 0:
        raise ValueError(
            "file size and count must be positive; multipart overhead cannot be negative"
        )

    proposed_limit = (file_limit * file_count) + overhead
    return set_request_content_limit(proposed_limit)
