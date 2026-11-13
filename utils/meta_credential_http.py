"""Narrow HTTP privacy boundary for the reviewed Meta credential operation."""
from __future__ import annotations

import logging
import os

from flask import current_app, has_request_context, jsonify, request

PREFIX = "/api/internal/meta-credentials"
ENDPOINTS = frozenset({"meta_credential_admin.plan", "meta_credential_admin.apply", "meta_credential_admin.status",
                       "meta_credential_admin.inspect_junin_webhook"})
MAX_BODY_BYTES = 16_384
PANEL_ORIGINS = frozenset({"https://chatboc.ar", "https://www.chatboc.ar"})


def is_meta_credential_admin_request(req=None):
    req = request if req is None else req
    return req.endpoint in ENDPOINTS


def _sensitive_path():
    return has_request_context() and (request.path == PREFIX or request.path.startswith(PREFIX + "/"))


def runtime_is_production():
    # These variables are consumed only in the actual deployed web process.
    # The operator endpoint cannot accept runtime identity from its caller.
    return (os.environ.get("VERCEL") == "1" and os.environ.get("VERCEL_ENV") == "production"
            and not current_app.config.get("TESTING"))


def response(payload, status=200):
    result = jsonify(payload)
    result.status_code = status
    result.headers["Cache-Control"] = "private, no-store"
    result.headers["Pragma"] = "no-cache"
    result.headers["X-Content-Type-Options"] = "nosniff"
    return result


def error(code, status, **extra):
    return response({"contract": "chatboc.meta_credential_operation.v1", "ok": False,
                     "reason_code": code, "retryable": False, **extra}, status)


def _install_log_redaction():
    previous = logging.getLogRecordFactory()
    if getattr(previous, "_chatboc_meta_redaction", False):
        return

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        if _sensitive_path():
            # Covers third-party HTTP/SQL DEBUG and exception records too.
            # Keep request diagnostics outside this path unchanged. Never
            # disable logging process-wide on concurrent Vercel requests.
            record.msg = "Meta credential administrative request (details suppressed)"
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return record

    factory._chatboc_meta_redaction = True
    logging.setLogRecordFactory(factory)


def register_meta_credential_http_boundary(app):
    _install_log_redaction()

    @app.before_request
    def meta_credential_boundary():
        if not _sensitive_path():
            return None
        if (not is_meta_credential_admin_request()
                or current_app.config.get("META_CREDENTIAL_ADMIN_ENABLED") is not True):
            return error("meta_operation_unavailable", 404)
        if not runtime_is_production():
            return error("meta_operation_production_runtime_required", 503)
        if request.query_string:
            return error("meta_operation_query_not_allowed", 400)
        origin = request.headers.get("Origin")
        if origin is not None and origin not in PANEL_ORIGINS:
            return error("meta_operation_origin_denied", 403)
        if request.method == "OPTIONS":
            if origin not in PANEL_ORIGINS or request.headers.get("Access-Control-Request-Method") != "POST":
                return error("meta_operation_origin_denied", 403)
            result = response({}, 204)
            result.headers["Access-Control-Allow-Origin"] = origin
            result.headers["Access-Control-Allow-Credentials"] = "true"
            result.headers["Access-Control-Allow-Methods"] = "POST"
            result.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            return result
        if request.method != "POST":
            return error("meta_operation_post_required", 405)
        if request.mimetype != "application/json" or request.headers.get("Content-Encoding"):
            return error("meta_operation_json_required", 415)
        if request.content_length is None or not 0 < request.content_length <= MAX_BODY_BYTES:
            return error("meta_operation_body_limit", 413)
        # No body is read here. Auth and tenant hooks skip only these exact
        # registered endpoints; the authenticated handler owns bounded parsing.
        return None

    @app.after_request
    def meta_credential_private_response(result):
        if _sensitive_path():
            result.headers["Cache-Control"] = "private, no-store"
            result.headers["Pragma"] = "no-cache"
            result.headers["X-Content-Type-Options"] = "nosniff"
            request.__dict__.pop("_cached_data", None)
            request.__dict__.pop("_cached_json", None)
        return result
