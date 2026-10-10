"""Inbound-only Twilio form signature validation.

This intentionally implements only the documented HMAC-SHA1 request-signature
boundary used by Twilio webhooks. Keeping it local prevents the isolated image
from shipping any outbound Twilio REST client while parity tests use the
official SDK as the oracle.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any
from urllib.parse import urlparse


def _values(params: Any, name: str) -> list[str]:
    try:
        values = params.getall(name)
    except AttributeError:
        try:
            values = params.getlist(name)
        except AttributeError:
            values = [params[name]]
    return [str(value) for value in values]


def _signature(token: bytes, uri: str, params: Any) -> str:
    material = uri
    if params:
        for name in sorted(set(params)):
            for value in sorted(set(_values(params, name))):
                material += str(name) + value
    digest = hmac.new(token, material.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii").strip()


def _with_port(uri: str) -> str:
    parsed = urlparse(uri)
    if parsed.port:
        return parsed.geturl()
    port = 443 if parsed.scheme == "https" else 80
    return parsed._replace(netloc=f"{parsed.netloc}:{port}").geturl()


def _without_port(uri: str) -> str:
    parsed = urlparse(uri)
    if not parsed.port:
        return parsed.geturl()
    # Preserve the exact netloc casing used by Twilio's official validator.
    # Public ingress settings independently reject credentials and IPv6.
    netloc = parsed.netloc.split(":")[0]
    return parsed._replace(netloc=netloc).geturl()


class TwilioInboundSignatureValidator:
    """Validate form signatures with official with/without-port semantics."""

    def __init__(self, token: str):
        self._token = str(token).encode("utf-8")

    def validate(self, uri: str, params: Any, signature: str) -> bool:
        supplied = str(signature or "")
        without_port = _signature(self._token, _without_port(uri), params)
        with_port = _signature(self._token, _with_port(uri), params)
        return hmac.compare_digest(without_port, supplied) or hmac.compare_digest(
            with_port,
            supplied,
        )
