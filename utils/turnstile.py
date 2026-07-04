import os

import requests
from flask import current_app


VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TURNSTILE_TOKEN_HEADER = "X-Turnstile-Token"
TURNSTILE_TOKEN_FIELDS = (
    "turnstile_token",
    "cloudflare_turnstile_token",
    "cf-turnstile-response",
    "cf_turnstile_response",
)


def _configured_secret() -> str:
    return (
        current_app.config.get("CLOUDFLARE_TURNSTILE_SECRET_KEY")
        or current_app.config.get("TURNSTILE_SECRET_KEY")
        or os.getenv("CLOUDFLARE_TURNSTILE_SECRET_KEY")
        or os.getenv("TURNSTILE_SECRET_KEY")
        or ""
    ).strip()


def turnstile_is_configured() -> bool:
    return bool(_configured_secret())


def turnstile_enforce_public_intake() -> bool:
    value = (
        current_app.config.get("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE")
        or os.getenv("CLOUDFLARE_TURNSTILE_ENFORCE_PUBLIC_INTAKE")
        or ""
    )
    return str(value).strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def turnstile_public_intake_contract(
    *,
    surface: str = "public_intake",
    status: str | None = None,
    reason: str | None = None,
    retryable: bool | None = None,
    reset_required: bool | None = None,
) -> dict:
    """Frontend contract for anonymous public intake protection.

    The token is still validated by the backend. This payload only lets the
    client render, retry and explain the challenge consistently across
    marketplace, widget, claims and survey flows.
    """

    enforced = turnstile_enforce_public_intake()
    configured = turnstile_is_configured()
    normalized_status = str(status or ("required" if enforced else "optional")).strip() or "optional"
    if retryable is None:
        retryable = normalized_status in {"token_missing", "verification_failed", "expired", "invalid"}
    if reset_required is None:
        reset_required = normalized_status in {"verification_failed", "expired", "invalid"}

    payload = {
        "contract_version": "cloudflare.turnstile.public_intake.v1",
        "provider": "cloudflare_turnstile",
        "surface": surface,
        "status": normalized_status,
        "configured": configured,
        "enforced": enforced,
        "required": enforced,
        "token_header": TURNSTILE_TOKEN_HEADER,
        "token_fields": list(TURNSTILE_TOKEN_FIELDS),
        "retryable": bool(retryable),
        "reset_required": bool(reset_required),
    }
    if reason:
        payload["reason"] = reason
    return payload


def verify_turnstile(
    response_token: str | None,
    *,
    remote_ip: str | None = None,
    idempotency_key: str | None = None,
) -> bool:
    """Verify a Cloudflare Turnstile token when configured.

    When the secret is absent we intentionally return True, matching the
    existing reCAPTCHA development behavior. Enforcement is decided by callers.
    """

    secret = _configured_secret()
    if not secret:
        current_app.logger.warning(
            "CLOUDFLARE_TURNSTILE_SECRET_KEY no configurada; omitiendo verificacion Turnstile"
        )
        return True

    token = str(response_token or "").strip()
    if not token or token.lower() in {"undefined", "null"}:
        current_app.logger.warning("Token Turnstile ausente en endpoint publico protegido")
        return False

    payload = {
        "secret": secret,
        "response": token,
    }
    if remote_ip:
        payload["remoteip"] = remote_ip
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key[:255]

    try:
        resp = requests.post(VERIFY_URL, data=payload, timeout=5)
        if resp.status_code != 200:
            current_app.logger.warning(
                "Fallo en la verificacion Turnstile: HTTP %s", resp.status_code
            )
            return False
        result = resp.json()
        return result.get("success") is True
    except Exception as exc:
        current_app.logger.error("Error al verificar Turnstile: %s", exc)
        return False
