"""Shared security gate for every public survey response alias.

The durable response receipt is checked by each route before this gate.  A
committed replay therefore remains readable without consuming one-shot
Turnstile tokens or rate-limit capacity.  Every *new* response must pass this
gate before ``save_respuesta`` is called.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import time
from typing import Any, Callable, Mapping

from flask import current_app, request
from limits import parse

from extensions import limiter
from services.encuestas_service import EncuestaError, get_public_encuesta
from utils.turnstile import (
    TURNSTILE_TOKEN_FIELDS,
    TURNSTILE_TOKEN_HEADER,
    turnstile_enforce_public_intake,
    turnstile_is_configured,
    turnstile_public_intake_contract,
    verify_turnstile,
)


_DEFAULT_PUBLIC_RESPONSE_RATE_LIMIT = 150
_DEFAULT_PUBLIC_RESPONSE_RATE_PERIOD = 60
_RATE_LIMIT_NAMESPACE = "public-survey-response"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def public_survey_client_ip() -> str:
    """Return the proxy-provided client address used by every survey alias."""

    cloudflare_ip = str(request.headers.get("CF-Connecting-IP") or "").strip()
    if cloudflare_ip:
        return cloudflare_ip
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip() or "0.0.0.0"
    return request.remote_addr or "0.0.0.0"


def public_survey_turnstile_token(payload: Mapping[str, Any] | None) -> str | None:
    """Resolve the canonical Turnstile token without route-specific parsers."""

    header_value = request.headers.get(TURNSTILE_TOKEN_HEADER)
    if header_value:
        return str(header_value)
    if isinstance(payload, Mapping):
        for field in TURNSTILE_TOKEN_FIELDS:
            value = payload.get(field)
            if value:
                return str(value)
    query_value = request.args.get("turnstile_token")
    return str(query_value) if query_value else None


def _rate_settings() -> tuple[int, int]:
    limit = current_app.config.get("PUBLIC_ENCUESTAS_RATE_LIMIT")
    period = current_app.config.get("PUBLIC_ENCUESTAS_RATE_PERIOD")
    if limit is None:
        limit = os.getenv("PUBLIC_ENCUESTAS_RATE_LIMIT")
    if period is None:
        period = os.getenv("PUBLIC_ENCUESTAS_RATE_PERIOD")
    return (
        _coerce_positive_int(limit, _DEFAULT_PUBLIC_RESPONSE_RATE_LIMIT),
        _coerce_positive_int(period, _DEFAULT_PUBLIC_RESPONSE_RATE_PERIOD),
    )


def _rate_scope_key(
    survey_token: str,
    *,
    client_ip: str,
) -> str:
    # EncEncuesta.slug is globally unique.  Excluding caller-controlled tenant
    # hints makes V2, legacy and PWA consume the same bucket even if a client
    # adds, removes or forges a tenant query parameter to try to evade it.
    material = f"{survey_token}\x1f{client_ip}".encode("utf-8", "replace")
    return hashlib.sha256(material).hexdigest()


def _consume_rate_limit(
    survey_token: str,
    *,
    client_ip: str,
) -> dict[str, Any]:
    """Consume shared Flask-Limiter capacity and fail closed on storage errors.

    ``limiter.limiter`` uses the application's configured storage backend.  It
    therefore shares counters across processes when ``RATELIMIT_STORAGE_URI``
    points at a shared backend, without claiming that Redis is present in every
    environment.
    """

    limit, period = _rate_settings()
    item = parse(f"{limit} per {period} seconds")
    scope_key = _rate_scope_key(
        survey_token,
        client_ip=client_ip,
    )
    identifiers = (_RATE_LIMIT_NAMESPACE, scope_key)
    try:
        strategy = limiter.limiter
        allowed = bool(strategy.hit(item, *identifiers))
        window = strategy.get_window_stats(item, *identifiers)
        remaining = max(0, int(window.remaining))
        reset_after = max(1, int(float(window.reset_time) - time.time() + 0.999))
    except Exception:
        current_app.logger.exception(
            "[encuestas] Shared public survey rate limiter unavailable; intake denied"
        )
        return {
            "allowed": False,
            "available": False,
            "limit": limit,
            "remaining": 0,
            "window_seconds": period,
            "retry_after_seconds": max(1, period),
            "reset_after_seconds": max(1, period),
        }

    return {
        "allowed": allowed,
        "available": True,
        "limit": limit,
        "remaining": remaining,
        "window_seconds": period,
        "retry_after_seconds": 0 if allowed else reset_after,
        "reset_after_seconds": reset_after,
    }


def survey_security_contract(
    *,
    status: str | None = None,
    reason: str | None = None,
    retryable: bool | None = None,
    reset_required: bool | None = None,
) -> dict[str, Any]:
    return turnstile_public_intake_contract(
        surface="survey_public_response",
        status=status,
        reason=reason,
        retryable=retryable,
        reset_required=reset_required,
    )


def survey_frontend_security_contract(
    security: Mapping[str, Any],
    *,
    render_as: str = "public_survey_response",
) -> dict[str, Any]:
    can_retry = bool(security.get("retryable"))
    reset_turnstile = bool(security.get("reset_required"))
    return {
        "contract_version": "surveys.public_frontend.v2",
        "render_as": render_as,
        "security_provider": security.get("provider"),
        "turnstile": {
            "enabled": bool(security.get("configured") or security.get("enforced")),
            "required": bool(security.get("required")),
            "status": security.get("status"),
            "surface": security.get("surface"),
            "token_header": security.get("token_header"),
            "token_fields": security.get("token_fields") or [],
            "can_retry": can_retry,
            "reset_required": reset_turnstile,
        },
        "can_retry": can_retry,
        "reset_turnstile": reset_turnstile,
    }


@dataclass(frozen=True)
class PublicSurveyIntakeDecision:
    allowed: bool
    rate_limit: dict[str, Any]
    security: dict[str, Any] | None = None
    status_code: int | None = None
    reason_code: str | None = None
    message: str | None = None
    action_hint: str | None = None

    def error_payload(self) -> dict[str, Any]:
        if self.allowed or self.status_code is None or not self.reason_code or not self.message:
            raise RuntimeError("public_survey_intake_decision_has_no_error")

        retryable = bool(self.security and self.security.get("retryable"))
        payload: dict[str, Any] = {
            "contract_version": "surveys.public_response.v2",
            "ok": False,
            "status_code": self.status_code,
            "reason_code": self.reason_code,
            "retryable": retryable,
            "action_hint": self.action_hint or "retry_later",
            "message": self.message,
            "error": {"code": self.status_code, "message": self.message},
        }
        if self.rate_limit:
            payload["rate_limit"] = {
                "limit": self.rate_limit.get("limit"),
                "remaining": self.rate_limit.get("remaining"),
                "window_seconds": self.rate_limit.get("window_seconds"),
                "reset_after_seconds": self.rate_limit.get("reset_after_seconds"),
                "retry_after_seconds": self.rate_limit.get("retry_after_seconds"),
            }
        if self.security is not None:
            payload["security"] = self.security
            payload["frontend_contract"] = survey_frontend_security_contract(
                self.security,
                render_as="public_survey_security_error",
            )
        return payload


def enforce_public_survey_replay_scope(
    replay: Any,
    *,
    preferred_tenant_id: int | str | None,
) -> PublicSurveyIntakeDecision | None:
    """Reject a durable receipt replay when its tenant does not match.

    This check intentionally runs before one-shot security controls, allowing a
    correctly scoped committed replay while preventing cross-tenant receipt
    disclosure.  It does not query active survey state, so a valid replay still
    works after the survey closes.
    """

    if replay is None or preferred_tenant_id is None:
        return None
    try:
        replay_tenant_id = int(getattr(replay, "tenant_id"))
        requested_tenant_id = int(preferred_tenant_id)
    except (TypeError, ValueError, AttributeError):
        replay_tenant_id = None
        requested_tenant_id = None
    if replay_tenant_id is not None and replay_tenant_id == requested_tenant_id:
        return None
    return PublicSurveyIntakeDecision(
        allowed=False,
        rate_limit={},
        status_code=404,
        reason_code="survey_not_found",
        message="Encuesta no encontrada",
        action_hint="check_survey_link",
    )


def enforce_public_survey_intake(
    survey_token: str,
    payload: Mapping[str, Any] | None,
    *,
    preferred_tenant_id: int | str | None,
    request_id: str | None,
    synthetic: bool = False,
    verifier: Callable[..., bool] | None = None,
) -> PublicSurveyIntakeDecision:
    """Apply the V2 public survey security contract to a fresh submission."""

    client_ip = public_survey_client_ip()
    rate_limit = _consume_rate_limit(
        survey_token,
        client_ip=client_ip,
    )
    if not rate_limit.get("available", False):
        return PublicSurveyIntakeDecision(
            allowed=False,
            rate_limit=rate_limit,
            status_code=503,
            reason_code="survey_rate_limit_unavailable",
            message="La proteccion de respuestas no esta disponible. Intenta nuevamente mas tarde.",
            action_hint="retry_later",
        )
    if not rate_limit.get("allowed", False):
        return PublicSurveyIntakeDecision(
            allowed=False,
            rate_limit=rate_limit,
            status_code=429,
            reason_code="rate_limited",
            message="Demasiadas respuestas desde esta IP. Intenta mas tarde.",
            action_hint="retry_later",
        )

    turnstile_token = public_survey_turnstile_token(payload)
    enforce_turnstile = turnstile_enforce_public_intake()
    if enforce_turnstile and not turnstile_is_configured():
        security = survey_security_contract(
            status="misconfigured",
            reason="missing_secret",
            retryable=False,
            reset_required=False,
        )
        return PublicSurveyIntakeDecision(
            allowed=False,
            rate_limit=rate_limit,
            security=security,
            status_code=503,
            reason_code="turnstile_no_configurado",
            message="La verificacion de seguridad no esta disponible. Intenta nuevamente mas tarde.",
            action_hint="retry_later",
        )

    if turnstile_token or enforce_turnstile:
        token_verifier = verifier or verify_turnstile
        if not token_verifier(
            turnstile_token,
            remote_ip=request.headers.get("CF-Connecting-IP") or client_ip,
            idempotency_key=request_id,
        ):
            security = survey_security_contract(
                status="verification_failed",
                reason="invalid_or_expired_token",
                retryable=True,
                reset_required=True,
            )
            return PublicSurveyIntakeDecision(
                allowed=False,
                rate_limit=rate_limit,
                security=security,
                status_code=400,
                reason_code="turnstile_verificacion_fallida",
                message="No pudimos validar la verificacion de seguridad. Intenta nuevamente.",
                action_hint="retry_security_challenge",
            )

    security = survey_security_contract(
        status="verified" if (turnstile_token or enforce_turnstile) else "not_required",
        reason="anonymous_public_survey",
        retryable=False,
        reset_required=False,
    )

    if preferred_tenant_id is not None and not synthetic:
        try:
            encuesta = get_public_encuesta(
                survey_token,
                preferred_tenant_id=int(preferred_tenant_id),
            )
        except EncuestaError as exc:
            return PublicSurveyIntakeDecision(
                allowed=False,
                rate_limit=rate_limit,
                security=security,
                status_code=exc.status_code,
                reason_code=str(exc.payload.get("reason_code") or "survey_not_found"),
                message=exc.message,
                action_hint=str(exc.payload.get("action_hint") or "check_survey_link"),
            )
        try:
            survey_tenant_id = int(encuesta.tenant_id)
            requested_tenant_id = int(preferred_tenant_id)
        except (TypeError, ValueError, AttributeError):
            survey_tenant_id = None
            requested_tenant_id = None
        if survey_tenant_id is None or survey_tenant_id != requested_tenant_id:
            return PublicSurveyIntakeDecision(
                allowed=False,
                rate_limit=rate_limit,
                security=security,
                status_code=404,
                reason_code="survey_not_found",
                message="Encuesta no encontrada",
                action_hint="check_survey_link",
            )

    return PublicSurveyIntakeDecision(
        allowed=True,
        rate_limit=rate_limit,
        security=security,
    )


def attach_public_survey_rate_limit_headers(response, telemetry: Mapping[str, Any]):
    response.headers["X-RateLimit-Limit"] = str(telemetry.get("limit", ""))
    response.headers["X-RateLimit-Remaining"] = str(telemetry.get("remaining", ""))
    response.headers["X-RateLimit-Window"] = str(telemetry.get("window_seconds", ""))
    if not telemetry.get("allowed", True):
        response.headers["Retry-After"] = str(telemetry.get("retry_after_seconds", 1))
    return response
