from __future__ import annotations

import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any

import jwt
from flask import current_app


FINANCE_WEBVIEW_TOKEN_AUDIENCE = "chatboc-finance-webview"
FINANCE_WEBVIEW_TOKEN_ISSUER = "chatboc-finance-webview-access"
FINANCE_WEBVIEW_TOKEN_SCOPE = "finance_webview:access"
DEFAULT_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS = 900
MIN_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS = 60
MAX_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS = 3600

_JTI_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
_BINDING_KEY_CONTEXT = b"chatboc-finance-webview-binding-v1"


class FinanceWebviewAccessError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _secret_key() -> str:
    secret = str(
        current_app.config.get("FINANCE_WEBVIEW_TOKEN_SECRET")
        or current_app.config.get("SECRET_KEY")
        or ""
    ).strip()
    if not secret:
        raise FinanceWebviewAccessError("secret_key_not_configured")
    return secret


def _context_values(tenant_slug: Any, flow: Any, operation_code: Any) -> dict[str, str]:
    context = {
        "tenant_slug": str(tenant_slug or "").strip().lower(),
        "flow": str(flow or "").strip().lower(),
        "operation_code": str(operation_code or "").strip(),
    }
    if not all(context.values()):
        raise FinanceWebviewAccessError("invalid_finance_context")
    return context


def _canonical_amount(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value).strip().replace(",", "."))
        if not parsed.is_finite() or parsed <= 0:
            return None
        return f"{parsed.quantize(Decimal('0.01'))}"
    except (InvalidOperation, TypeError, ValueError):
        return None


def _canonical_text(value: Any, *, limit: int) -> str | None:
    cleaned = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return cleaned[:limit] or None


def normalize_finance_webview_values(
    *,
    amount: Any = None,
    currency: Any = "ARS",
    contact_key: Any = None,
) -> dict[str, str | None]:
    return {
        "amount": _canonical_amount(amount),
        "currency": (str(currency or "ARS").strip().upper() or "ARS")[:8],
        "contact_key": _canonical_text(contact_key, limit=160),
    }


def _binding_hash(
    *,
    tenant_slug: str,
    flow: str,
    operation_code: str,
    amount: Any = None,
    currency: Any = "ARS",
    contact_key: Any = None,
) -> str:
    context = _context_values(tenant_slug, flow, operation_code)
    values = normalize_finance_webview_values(
        amount=amount,
        currency=currency,
        contact_key=contact_key,
    )
    canonical = json.dumps(
        {**context, **values},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    secret = _secret_key().encode("utf-8")
    binding_key = hmac.new(secret, _BINDING_KEY_CONTEXT, sha256).digest()
    return hmac.new(binding_key, canonical, sha256).hexdigest()


def _ttl_seconds(value: Any) -> int:
    try:
        return max(
            MIN_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS,
            min(int(value), MAX_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS),
        )
    except (TypeError, ValueError):
        return DEFAULT_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS


def issue_finance_webview_token(
    tenant_slug: str,
    flow: str,
    operation_code: str,
    *,
    amount: Any = None,
    currency: Any = "ARS",
    contact_key: Any = None,
    ttl_seconds: int | None = None,
) -> str:
    context = _context_values(tenant_slug, flow, operation_code)
    configured_ttl = (
        ttl_seconds
        if ttl_seconds is not None
        else current_app.config.get(
            "FINANCE_WEBVIEW_TOKEN_TTL_SECONDS",
            DEFAULT_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS,
        )
    )
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "iss": FINANCE_WEBVIEW_TOKEN_ISSUER,
            "aud": FINANCE_WEBVIEW_TOKEN_AUDIENCE,
            "scope": FINANCE_WEBVIEW_TOKEN_SCOPE,
            **context,
            "binding_hash": _binding_hash(
                **context,
                amount=amount,
                currency=currency,
                contact_key=contact_key,
            ),
            "iat": now,
            "exp": now + timedelta(seconds=_ttl_seconds(configured_ttl)),
            "jti": secrets.token_urlsafe(24),
        },
        _secret_key(),
        algorithm="HS256",
    )
    return token.decode("utf-8") if isinstance(token, bytes) else str(token)


def verify_finance_webview_token(
    token: str,
    *,
    expected_tenant_slug: str,
    expected_flow: str,
    expected_operation_code: str,
    amount: Any = None,
    currency: Any = "ARS",
    contact_key: Any = None,
) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise FinanceWebviewAccessError("missing_access_token")
    if len(token) > 8192:
        raise FinanceWebviewAccessError("invalid_access_token")

    try:
        payload = jwt.decode(
            token.strip(),
            _secret_key(),
            algorithms=["HS256"],
            audience=FINANCE_WEBVIEW_TOKEN_AUDIENCE,
            issuer=FINANCE_WEBVIEW_TOKEN_ISSUER,
            options={
                "require": [
                    "aud",
                    "binding_hash",
                    "exp",
                    "flow",
                    "iat",
                    "iss",
                    "jti",
                    "operation_code",
                    "scope",
                    "tenant_slug",
                ]
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise FinanceWebviewAccessError("expired_access_token") from exc
    except jwt.InvalidTokenError as exc:
        raise FinanceWebviewAccessError("invalid_access_token") from exc

    if payload.get("scope") != FINANCE_WEBVIEW_TOKEN_SCOPE:
        raise FinanceWebviewAccessError("invalid_access_scope")

    expected_context = _context_values(
        expected_tenant_slug,
        expected_flow,
        expected_operation_code,
    )
    claimed_context = _context_values(
        payload.get("tenant_slug"),
        payload.get("flow"),
        payload.get("operation_code"),
    )
    for key in ("tenant_slug", "flow", "operation_code"):
        if not hmac.compare_digest(claimed_context[key], expected_context[key]):
            raise FinanceWebviewAccessError(f"{key}_mismatch")

    jti = str(payload.get("jti") or "")
    if not _JTI_PATTERN.fullmatch(jti):
        raise FinanceWebviewAccessError("invalid_access_jti")

    try:
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except (TypeError, ValueError) as exc:
        raise FinanceWebviewAccessError("invalid_access_token") from exc
    if expires_at <= issued_at or expires_at - issued_at > MAX_FINANCE_WEBVIEW_TOKEN_TTL_SECONDS:
        raise FinanceWebviewAccessError("invalid_access_lifetime")

    expected_binding = _binding_hash(
        **expected_context,
        amount=amount,
        currency=currency,
        contact_key=contact_key,
    )
    claimed_binding = str(payload.get("binding_hash") or "")
    if not hmac.compare_digest(claimed_binding, expected_binding):
        raise FinanceWebviewAccessError("finance_values_mismatch")

    return {
        **payload,
        **claimed_context,
        "jti": jti,
    }
