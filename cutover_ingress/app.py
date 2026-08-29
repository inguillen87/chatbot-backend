"""Minimal Flask boundary for the independent cutover ingress service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from flask import Flask, Response, jsonify, request
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from twilio.request_validator import RequestValidator

from .core import (
    CutoverIngressConflict,
    CutoverIngressIntegrityError,
    CutoverIngressSettings,
    CutoverIngressStore,
)


ACK_BODY = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"


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
    validator = RequestValidator(resolved.twilio_auth_token)

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
        try:
            with resolved_engine.connect() as connection:
                connection.execute(select(1)).scalar_one()
        except SQLAlchemyError:
            return _response("unavailable", 503)
        return jsonify(
            {
                "contract_version": "chatboc.cutover_ingress.health.v1",
                "database": "reachable",
                "mode": "buffer_only",
                "providers_enabled": False,
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
        try:
            store.persist(payload)
        except CutoverIngressConflict:
            return _response("conflicting replay", 409)
        except CutoverIngressIntegrityError:
            return _response("invalid webhook", 422)
        except SQLAlchemyError:
            # No ACK is emitted when durability is uncertain.
            return _response("queue unavailable", 503)
        except Exception:
            # Fail closed without leaking provider payload, database details,
            # key identifiers, or exception text.
            return _response("queue unavailable", 503)
        return _response(ACK_BODY, 200, content_type="application/xml")

    return app
