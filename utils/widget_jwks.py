"""Public-only widget key publication; signing secrets never belong in JWKS."""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt import algorithms
from jwt.exceptions import PyJWTError


def build_public_widget_jwks(config: Mapping[str, Any]) -> dict[str, list[dict[str, str]]]:
    """Return public RS256/ES256 verification keys, or an empty, closed set.

    HS256 signing remains supported server-side for legacy sessions. It has
    no public verification key. Do not read a secret/private-key fallback.
    Private PEMs in the public-key setting are rejected, not serialized.
    """
    alg = str(config.get("WIDGET_JWT_ALG") or "HS256").strip().upper()
    if alg not in {"RS256", "ES256"}:
        return {"keys": []}
    public_key = config.get("WIDGET_JWT_PUBLIC_KEY")
    if not isinstance(public_key, (str, bytes)) or not public_key:
        return {"keys": []}
    try:
        implementation = algorithms.get_default_algorithms()[alg]
        key = implementation.prepare_key(public_key)
        if alg == "RS256":
            if not isinstance(key, rsa.RSAPublicKey) or key.key_size < 2048:
                return {"keys": []}
            fields = ("kty", "n", "e")
        else:
            if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
                return {"keys": []}
            fields = ("kty", "crv", "x", "y")
        raw = json.loads(implementation.to_jwk(key))
        # An allowlist also protects against future serializer changes.
        if not isinstance(raw, dict) or not all(isinstance(raw.get(k), str) and raw[k] for k in fields):
            return {"keys": []}
        jwk = {field: raw[field] for field in fields}
    except (ValueError, TypeError, KeyError, PyJWTError, UnsupportedAlgorithm):
        return {"keys": []}
    jwk.update({"use": "sig", "alg": alg, "kid": str(config.get("WIDGET_JWT_KID") or f"widget-{alg.lower()}")})
    return {"keys": [jwk]}
