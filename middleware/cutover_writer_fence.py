"""Opt-in HTTP writer fence for controlled database cutovers.

The fence is deliberately disabled by default.  When enabled, every unsafe
HTTP method is rejected before authentication, tenant resolution, uploads, or
webhook handlers can mutate state.  The same flag is also enforced by internal
cron routes, standalone workers and mutating cron commands.
"""

from __future__ import annotations

from flask import Flask, current_app, jsonify, request

from cutover_writer_fence import (
    cutover_writer_fence_enabled,
    is_cutover_read_only_view,
    is_cutover_writer_view,
)
from global_writer_authority import GLOBAL_WRITER_AUTHORITY_CONTRACT
from services.global_writer_authority import evaluate_global_writer_authority


_READ_METHODS = frozenset({"GET", "HEAD"})


def register_cutover_writer_fence(app: Flask) -> None:
    """Register the fail-closed HTTP mutation gate on ``app``."""

    @app.before_request
    def enforce_cutover_writer_fence():
        method = request.method.upper()
        if method == "OPTIONS":
            return None
        view = current_app.view_functions.get(request.endpoint or "")
        # A contradictory dual marker must fail closed.  This prevents a
        # future refactor from accidentally exempting a handler that is also
        # known to mutate or poll a provider.
        if is_cutover_read_only_view(view) and not is_cutover_writer_view(view):
            return None
        is_writer = True
        if method in _READ_METHODS:
            if not is_cutover_writer_view(view):
                is_writer = False
        if not is_writer:
            return None

        if cutover_writer_fence_enabled(current_app.config):
            payload = {
                "contract_version": "cutover.writer_fence.v1",
                "status": "maintenance",
                "reason_code": "cutover_writer_fence_enabled",
                "retryable": True,
            }
        else:
            decision = evaluate_global_writer_authority(current_app.config)
            if decision.allowed:
                return None
            payload = {
                "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
                "status": "maintenance",
                "reason_code": decision.reason_code,
                "retryable": True,
            }

        response = jsonify(payload)
        response.status_code = 503
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "60"
        return response
