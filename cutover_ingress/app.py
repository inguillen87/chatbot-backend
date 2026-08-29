"""Minimal Flask boundary for the independent cutover ingress service."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
import threading
import time
from typing import Any, Optional

from flask import Flask, Response, jsonify, request
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError

from .core import (
    CutoverIngressConfigurationError,
    CutoverIngressConflict,
    CutoverIngressIntegrityError,
    CutoverIngressSettings,
    CutoverIngressStore,
)
from .twilio_signature import TwilioInboundSignatureValidator


ACK_BODY = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"


def _runtime_source_sha256() -> str:
    digest = hashlib.sha256()
    package_root = Path(__file__).resolve().parent
    for path in sorted(package_root.glob("*.py"), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _response(body: str, status: int, *, content_type: str = "text/plain") -> Response:
    response = Response(body, status=status, content_type=content_type)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if status >= 500:
        response.headers["Retry-After"] = "15"
    return response


def create_cutover_ingress_app(
    settings: Optional[CutoverIngressSettings] = None,
    *,
    engine: Optional[Engine] = None,
) -> Flask:
    """Build an ingress-only app; missing or stale schema aborts startup."""

    resolved = settings or CutoverIngressSettings.from_environ()
    resolved_engine = engine or resolved.build_engine()
    store = CutoverIngressStore(resolved_engine, resolved)
    validator = TwilioInboundSignatureValidator(resolved.twilio_auth_token)
    source_sha256 = _runtime_source_sha256()
    health_lock = threading.Lock()
    health_state: dict[str, Any] = {"checked_at": 0.0, "reachable": False}

    app = Flask("chatboc-cutover-ingress")
    app.config.update(
        MAX_CONTENT_LENGTH=resolved.max_payload_bytes * 2,
        JSON_SORT_KEYS=True,
        PROPAGATE_EXCEPTIONS=False,
    )
    # Exposed for a separately scheduled replay worker and focused tests.  It
    # is not attached to the primary Flask-SQLAlchemy extension.
    app.extensions["cutover_ingress_store"] = store

    @app.after_request
    def _security_headers(response: Response) -> Response:
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    @app.get("/health")
    def health() -> Response:
        now = time.monotonic()
        if now - float(health_state["checked_at"]) >= 5.0:
            with health_lock:
                now = time.monotonic()
                if now - float(health_state["checked_at"]) >= 5.0:
                    try:
                        with resolved_engine.connect() as connection:
                            connection.execute(select(1)).scalar_one()
                    except SQLAlchemyError:
                        health_state.update(checked_at=now, reachable=False)
                    else:
                        health_state.update(checked_at=now, reachable=True)
        if health_state["reachable"] is not True:
            return _response("unavailable", 503)
        return jsonify(
            {
                "contract_version": "chatboc.cutover_ingress.health.v1",
                "database": "reachable",
                "database_check_ttl_seconds": 5,
                "mode": "buffer_only",
                "providers_enabled": False,
                "source_sha256": source_sha256,
                "status": "buffer_ready",
            }
        )

    @app.post("/webhook/whatsapp")
    def whatsapp_ingress() -> Response:
        signature = str(request.headers.get("X-Twilio-Signature") or "").strip()
        if not signature:
            return _response("forbidden", 403)

        try:
            valid_signature = validator.validate(
                resolved.public_webhook_url,
                request.form,
                signature,
            )
        except Exception:
            valid_signature = False
        if not valid_signature:
            return _response("forbidden", 403)

        # Twilio does not currently send duplicate form keys for this webhook.
        # Rejecting them avoids ambiguity between what was signed, encrypted,
        # and later replayed.
        if any(len(request.form.getlist(key)) != 1 for key in request.form.keys()):
            return _response("invalid webhook", 422)
        payload: Mapping[str, Any] = request.form.to_dict(flat=True)
        # This provider header is audit evidence only; MessageSid remains the
        # deduplication boundary. Read it only after the Twilio signature has
        # passed, and never copy the raw value into the signed form envelope.
        idempotency_token = request.headers.get("I-Twilio-Idempotency-Token")
        try:
            store.persist(payload, idempotency_token=idempotency_token)
        except CutoverIngressConflict:
            return _response("conflicting replay", 409)
        except CutoverIngressIntegrityError:
            return _response("invalid webhook", 422)
        except CutoverIngressConfigurationError:
            # If Twilio sends the header but the dedicated HMAC key is absent,
            # do not ACK or persist a partially auditable request.
            return _response("queue unavailable", 503)
        except SQLAlchemyError:
            # No ACK is emitted when durability is uncertain.
            return _response("queue unavailable", 503)
        except Exception:
            # Fail closed without leaking provider payload, database details,
            # key identifiers, or exception text.
            return _response("queue unavailable", 503)
        return _response(ACK_BODY, 200, content_type="application/xml")

    return app
