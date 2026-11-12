from __future__ import annotations

import math
import time
from typing import Any, Mapping

from flask import g


AUTH_ASSURANCE_CLAIM = "auth_assurance"
AUTH_ASSURANCE_CONTRACT_VERSION = "auth.assurance.v1"
AUTH_ASSURANCE_ERROR_CONTRACT_VERSION = "auth.assurance.error.v1"
STRICT_MFA = "strict_mfa"
DEFAULT_STRICT_MFA_MAX_AGE_SECONDS = 10 * 60

_CLERK_FVA_SOURCE = "clerk_v2_fva"
_VERIFIED_CLERK_CLAIMS_MARKER = "_chatboc_verified_clerk_session"


class AuthAssuranceError(ValueError):
    def __init__(
        self,
        reason_code: str,
        message: str,
        *,
        clerk_error: dict[str, Any],
        no_retry: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message
        self.clerk_error = clerk_error
        self.no_retry = no_retry

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "contract_version": AUTH_ASSURANCE_ERROR_CONTRACT_VERSION,
            "status_code": 403,
            "reason_code": self.reason_code,
            "retryable": False,
            "error": {"code": 403, "message": self.message},
            "clerk_error": self.clerk_error,
        }
        if self.no_retry:
            payload["no_retry"] = True
        return payload


def mark_verified_clerk_claims(claims: Mapping[str, Any]) -> dict[str, Any]:
    """Mark claims only after Clerk signature/session verification succeeds.

    The marker is process-local provenance. It is never copied into the
    Chatboc JWT; only the normalized assurance snapshot is signed there.
    """

    verified_claims = dict(claims)
    verified_claims[_VERIFIED_CLERK_CLAIMS_MARKER] = True
    return verified_claims


def _unavailable_snapshot(status: str) -> dict[str, Any]:
    return {
        "version": AUTH_ASSURANCE_CONTRACT_VERSION,
        "source": _CLERK_FVA_SOURCE,
        "status": status,
        "first_factor_verified_at": None,
        "second_factor_verified_at": None,
    }


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def build_clerk_auth_assurance_snapshot(
    clerk_claims: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Normalize Clerk v2 ``fva`` into signed, absolute verification times.

    ``fva`` values are ages in minutes at the Clerk token ``iat``. Normalizing
    them without first verifying the Clerk token would let browser-provided
    data become trusted assurance, so unmarked inputs are deliberately ignored.
    """

    claims = clerk_claims if isinstance(clerk_claims, Mapping) else {}
    if claims.get(_VERIFIED_CLERK_CLAIMS_MARKER) is not True:
        return _unavailable_snapshot("unverified_source")

    issued_at = _finite_number(claims.get("iat"))
    if issued_at is None or issued_at <= 0:
        return _unavailable_snapshot("malformed")

    fva = claims.get("fva")
    if fva is None:
        return _unavailable_snapshot("missing")
    if not isinstance(fva, (list, tuple)) or len(fva) != 2:
        return _unavailable_snapshot("malformed")

    first_age = _finite_number(fva[0])
    second_age = _finite_number(fva[1])
    if first_age is None or second_age is None:
        return _unavailable_snapshot("malformed")
    if first_age < -1 or second_age < -1:
        return _unavailable_snapshot("malformed")
    if (-1 < first_age < 0) or (-1 < second_age < 0):
        return _unavailable_snapshot("malformed")

    first_verified_at = (
        int(round(issued_at - (first_age * 60))) if first_age >= 0 else None
    )
    if second_age == -1:
        # Clerk documents -1 as "not verified". It does not prove whether the
        # factor is unenrolled or merely absent from this session, so request a
        # normal step-up and let Clerk choose the valid verification strategy.
        return _unavailable_snapshot("missing")
    if first_age == -1:
        return _unavailable_snapshot("malformed")

    return {
        "version": AUTH_ASSURANCE_CONTRACT_VERSION,
        "source": _CLERK_FVA_SOURCE,
        "status": "verified",
        "first_factor_verified_at": first_verified_at,
        "second_factor_verified_at": int(
            round(issued_at - (second_age * 60))
        ),
    }


def _step_up_required() -> AuthAssuranceError:
    return AuthAssuranceError(
        "step_up_required",
        "Esta accion sensible requiere verificar nuevamente ambos factores.",
        clerk_error={
            "type": "forbidden",
            "reason": "reverification-error",
            "metadata": {"reverification": STRICT_MFA},
        },
    )


def require_token_auth_assurance(
    token_payload: Mapping[str, Any] | None,
    *,
    requirement: str = STRICT_MFA,
    max_age_seconds: int = DEFAULT_STRICT_MFA_MAX_AGE_SECONDS,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """Fail closed unless a signed Chatboc token has recent Clerk MFA."""

    if requirement != STRICT_MFA:
        raise ValueError(f"Unsupported auth assurance requirement: {requirement}")
    if isinstance(max_age_seconds, bool) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")

    payload = token_payload if isinstance(token_payload, Mapping) else {}
    snapshot = payload.get(AUTH_ASSURANCE_CLAIM)
    if not isinstance(snapshot, Mapping):
        raise _step_up_required()
    if (
        snapshot.get("version") != AUTH_ASSURANCE_CONTRACT_VERSION
        or snapshot.get("source") != _CLERK_FVA_SOURCE
    ):
        raise _step_up_required()

    status = snapshot.get("status")
    if status != "verified":
        raise _step_up_required()

    first_verified_at = _finite_number(snapshot.get("first_factor_verified_at"))
    second_verified_at = _finite_number(snapshot.get("second_factor_verified_at"))
    if first_verified_at is None or second_verified_at is None:
        raise _step_up_required()

    now_value = time.time() if now_epoch is None else _finite_number(now_epoch)
    if now_value is None:
        raise ValueError("now_epoch must be a finite number")
    for verified_at in (first_verified_at, second_verified_at):
        elapsed = now_value - verified_at
        if elapsed < 0 or elapsed > max_age_seconds:
            raise _step_up_required()

    return dict(snapshot)


def require_request_auth_assurance(
    requirement: str = STRICT_MFA,
    *,
    max_age_seconds: int = DEFAULT_STRICT_MFA_MAX_AGE_SECONDS,
) -> dict[str, Any]:
    """Apply assurance to the signature-verified token loaded by auth helpers."""

    return require_token_auth_assurance(
        getattr(g, "token_payload", None),
        requirement=requirement,
        max_age_seconds=max_age_seconds,
    )
