"""Opt-in HTTP writer fence for controlled database cutovers.

The fence is deliberately disabled by default.  When enabled, every unsafe
HTTP method is rejected before authentication, tenant resolution, uploads, or
webhook handlers can mutate state.  The same flag is also enforced by internal
cron routes, standalone workers and mutating cron commands.
"""

from __future__ import annotations

import sys
from threading import Lock

from flask import Flask, current_app, jsonify, request

from cutover_writer_fence import (
    cutover_writer_fence_enabled,
    is_cutover_read_only_view,
    is_cutover_writer_view,
)
from global_writer_authority import GLOBAL_WRITER_AUTHORITY_CONTRACT, global_writer_authority_enabled
from services.global_writer_authority import (
    GlobalWriterAuthorityTransitionError,
    HTTP_REQUEST_LEASE_ENVIRON,
    evaluate_global_writer_authority,
    global_writer_authority_lease,
)


_READ_METHODS = frozenset({"GET", "HEAD"})
_LEASE_STATE = HTTP_REQUEST_LEASE_ENVIRON


class _RequestLease:
    def __init__(self):
        self.manager = None
        self.lease = None
        self.lock = Lock()

    def release(self, error=(None, None, None)):
        with self.lock:
            manager, self.manager = self.manager, None
        if manager is not None:
            manager.__exit__(*error)


class _LeaseBoundResponse:
    """Release only after the entire WSGI iterable and its close callbacks."""

    def __init__(self, iterable, state):
        self.iterable = iterable
        self.iterator = iter(iterable)
        self.state = state
        self.closed = False
        self.lock = Lock()

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            self.close()
            raise
        except BaseException:
            self._close(sys.exc_info())
            raise

    def close(self):
        self._close((None, None, None))

    def _close(self, error):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            close = getattr(self.iterable, "close", None)
            if close is not None:
                close()
        finally:
            close_error = sys.exc_info()
            self.state.release(close_error if close_error[0] is not None else error)


class _WriterLeaseMiddleware:
    def __init__(self, application):
        self.application = application

    def __call__(self, environ, start_response):
        state = _RequestLease()
        environ[_LEASE_STATE] = state
        try:
            iterable = self.application(environ, start_response)
            return _LeaseBoundResponse(iterable, state)
        except BaseException:
            state.release(sys.exc_info())
            raise


def register_cutover_writer_fence(app: Flask) -> None:
    """Register the fail-closed HTTP mutation gate on ``app``."""

    app.wsgi_app = _WriterLeaseMiddleware(app.wsgi_app)

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
            if decision.allowed and global_writer_authority_enabled(current_app.config):
                manager = global_writer_authority_lease(current_app.config, request_lifetime=True)
                try:
                    lease = manager.__enter__()
                except GlobalWriterAuthorityTransitionError as error:
                    decision = type(decision)(allowed=False, enabled=True, reason_code=error.reason_code)
                else:
                    decision = lease.decision
                    if decision.allowed:
                        state = request.environ[_LEASE_STATE]
                        state.manager, state.lease = manager, lease
                    else:
                        manager.__exit__(None, None, None)
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
