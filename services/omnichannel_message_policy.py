from __future__ import annotations

from typing import Any


OMNICHANNEL_REPLY_MAX_BODY_BYTES = 8 * 1024


class OmnichannelMessagePolicyError(ValueError):
    """A reply body violates the durable omnichannel message contract."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def normalize_omnichannel_reply_body(value: Any) -> str:
    """Normalize a reply and enforce the byte boundary used by every ingress."""

    if not isinstance(value, str):
        raise OmnichannelMessagePolicyError("reply_body_required")
    body = value.strip()
    if not body:
        raise OmnichannelMessagePolicyError("reply_body_required")
    if len(body.encode("utf-8")) > OMNICHANNEL_REPLY_MAX_BODY_BYTES:
        raise OmnichannelMessagePolicyError("reply_body_too_large")
    return body
