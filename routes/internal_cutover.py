"""Authenticated, read-only cutover evidence endpoints."""

from __future__ import annotations

import hmac
import os

from flask import Blueprint, jsonify, request

from cutover_writer_fence import cutover_read_only_view
from scripts.attest_vercel_runtime_credential import (
    build_runtime_attestation_envelope,
)
from services.provider_cutover_evidence import (
    CutoverEvidenceError,
    clean,
    environment_name,
)


internal_cutover_bp = Blueprint(
    "internal_cutover",
    __name__,
    url_prefix="/api/internal/cutover",
)

_AUTH_ENV = "CUTOVER_RUNTIME_ATTESTATION_BEARER_SECRET"


def _response(payload: dict, status_code: int):
    response = jsonify(payload)
    response.status_code = status_code
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _authorized() -> tuple[bool, str | None]:
    configured = clean(os.environ.get(_AUTH_ENV))
    if len(configured.encode("utf-8")) < 32:
        return False, None
    provided = request.headers.get("Authorization")
    if not isinstance(provided, str) or not provided:
        return False, None
    expected = f"Bearer {configured}"
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")), configured


@internal_cutover_bp.post("/runtime-credential-attestation")
@cutover_read_only_view
def runtime_credential_attestation():
    authorized, bearer_secret = _authorized()
    if not authorized or bearer_secret is None:
        return _response(
            {
                "contract_version": "internal.cutover.authorization.v1",
                "status": "unauthorized",
            },
            401,
        )
    challenge = request.get_json(silent=True)
    if not isinstance(challenge, dict):
        return _response(
            {
                "contract_version": "chatboc.runtime_credential_attestation.v1",
                "status": "blocked",
                "reason_code": "runtime_attestation_challenge_invalid",
            },
            409,
        )
    try:
        # Bearer authentication is a fourth independent secret.  Fail closed
        # if an operator accidentally aliases or duplicates it with evidence
        # signing, credential binding, or the provider credential.
        protected_names = (
            challenge.get("signing_key_environment_variable"),
            challenge.get("credential_binding_key_environment_variable"),
            challenge.get("credential_environment_variable"),
        )
        protected_values = []
        for raw_name in protected_names:
            safe_name = environment_name(
                raw_name,
                reason_code="runtime_attestation_protected_environment_variable_invalid",
            )
            protected_values.append(clean(os.environ.get(safe_name)))
        if any(
            value and hmac.compare_digest(bearer_secret, value)
            for value in protected_values
        ):
            raise CutoverEvidenceError(
                "runtime_attestation_bearer_secret_must_be_independent"
            )
        envelope = build_runtime_attestation_envelope(challenge)
        return _response(envelope, 200)
    except CutoverEvidenceError as exc:
        return _response(
            {
                "contract_version": "chatboc.runtime_credential_attestation.v1",
                "status": "blocked",
                "reason_code": exc.reason_code,
                "read_only": True,
                "mutations_performed": False,
                "provider_calls_performed": False,
            },
            409,
        )
    except Exception as exc:  # pragma: no cover - defensive HTTP boundary
        return _response(
            {
                "contract_version": "chatboc.runtime_credential_attestation.v1",
                "status": "blocked",
                "reason_code": "runtime_attestation_unexpected_error",
                "error_type": type(exc).__name__,
                "read_only": True,
                "mutations_performed": False,
                "provider_calls_performed": False,
            },
            503,
        )


__all__ = ["internal_cutover_bp"]
