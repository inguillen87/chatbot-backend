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
    is_cutover_writer_view,
)


_READ_METHODS = frozenset({"GET", "HEAD"})


def register_cutover_writer_fence(app: Flask) -> None:
    """Register the fail-closed HTTP mutation gate on ``app``."""

    @app.before_request
    def enforce_cutover_writer_fence():
        method = request.method.upper()
        if method == "OPTIONS":
            return None
        if not cutover_writer_fence_enabled(current_app.config):
            return None
        if method in _READ_METHODS:
            view = current_app.view_functions.get(request.endpoint or "")
            if not is_cutover_writer_view(view):
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
