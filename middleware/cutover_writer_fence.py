"""Opt-in HTTP writer fence for controlled database cutovers.

The fence is deliberately disabled by default.  When enabled, every unsafe
HTTP method is rejected before authentication, tenant resolution, uploads, or
webhook handlers can mutate state.  Background workers and scheduled jobs have
independent ownership gates and must be quiesced separately by the operator.
"""

from __future__ import annotations

from flask import Flask, current_app, jsonify, request


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def register_cutover_writer_fence(app: Flask) -> None:
    """Register the fail-closed HTTP mutation gate on ``app``."""

    @app.before_request
    def enforce_cutover_writer_fence():
        if request.method.upper() in _SAFE_METHODS:
            return None
        if current_app.config.get("CUTOVER_WRITER_FENCE_ENABLED") is not True:
            return None

        response = jsonify(
            {
                "contract_version": "cutover.writer_fence.v1",
                "status": "maintenance",
                "reason_code": "cutover_writer_fence_enabled",
                "retryable": True,
            }
        )
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "60"
        return response

