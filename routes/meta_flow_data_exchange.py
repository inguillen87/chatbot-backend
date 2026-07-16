"""Flask Blueprint for Meta WhatsApp Flows Data Exchange."""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Mapping
from uuid import uuid4

from flask import Blueprint, Response, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from services.meta_flow_data_exchange import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    HARD_MAX_PAYLOAD_BYTES,
    DecryptedMetaFlowRequest,
    MetaFlowActionError,
    MetaFlowConfigurationError,
    MetaFlowEndpointConfig,
    MetaFlowEndpointError,
    MetaFlowResponseError,
    MetaFlowValidationError,
    build_encrypted_error_payload,
    coerce_endpoint_config,
    decrypt_flow_request,
    dispatch_flow_request,
    encrypt_flow_response,
    endpoint_fingerprint,
    is_request_signature_valid,
    normalize_action,
    parse_encrypted_envelope,
)


ConfigResolver = Callable[
    [str],
    MetaFlowEndpointConfig | Mapping[str, Any] | None,
]
_LOG_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def create_meta_flow_data_exchange_blueprint(
    *,
    config_resolver: ConfigResolver | None = None,
    name: str = "meta_flow_data_exchange",
) -> Blueprint:
    """Create a Blueprint; injection keeps tenant key lookup app-specific."""

    blueprint = Blueprint(
        name,
        __name__,
        url_prefix="/api/whatsapp/flows",
    )

    @blueprint.post("/data-exchange/<string:endpoint_id>")
    def data_exchange(endpoint_id: str):
        request_id = uuid4().hex
        endpoint_hash = endpoint_fingerprint(endpoint_id)

        if request.mimetype != "application/json":
            return _json_error(
                MetaFlowEndpointError(
                    "unsupported_media_type",
                    "Content-Type must be application/json.",
                    415,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )

        try:
            config = _resolve_config(endpoint_id, config_resolver)
        except _EndpointNotFound:
            return _json_error(
                MetaFlowEndpointError(
                    "endpoint_not_found",
                    "Flow endpoint is not configured.",
                    404,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        except MetaFlowEndpointError as exc:
            return _json_error(
                exc,
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        except Exception:
            return _json_error(
                MetaFlowConfigurationError("endpoint_resolver_failed"),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )

        body_limit = min(config.max_payload_bytes, _application_payload_limit())
        if request.content_length is not None and request.content_length > body_limit:
            return _json_error(
                MetaFlowEndpointError(
                    "payload_too_large",
                    "Flow request payload is too large.",
                    413,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        try:
            raw_body = request.stream.read(body_limit + 1)
        except RequestEntityTooLarge:
            return _json_error(
                MetaFlowEndpointError(
                    "payload_too_large",
                    "Flow request payload is too large.",
                    413,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        if len(raw_body) > body_limit:
            return _json_error(
                MetaFlowEndpointError(
                    "payload_too_large",
                    "Flow request payload is too large.",
                    413,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )

        if not is_request_signature_valid(
            raw_body,
            request.headers.get("X-Hub-Signature-256"),
            config,
        ):
            return _json_error(
                MetaFlowEndpointError(
                    "invalid_signature",
                    "Flow request signature validation failed.",
                    432,
                ),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        if not config.signature_enabled:
            _safe_log(
                logging.WARNING,
                event="signature_verification_disabled",
                request_id=request_id,
                endpoint_hash=endpoint_hash,
                status_code=0,
            )

        try:
            envelope = parse_encrypted_envelope(raw_body)
            decrypted_request = decrypt_flow_request(envelope, config)
        except MetaFlowEndpointError as exc:
            return _json_error(
                exc,
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )
        except Exception:
            return _json_error(
                MetaFlowValidationError("invalid_envelope"),
                request_id=request_id,
                endpoint_hash=endpoint_hash,
            )

        action = _safe_action(decrypted_request)
        try:
            response_payload = dispatch_flow_request(
                decrypted_request.payload,
                config,
                request_id=request_id,
            )
            encrypted_response = encrypt_flow_response(
                response_payload,
                decrypted_request,
                max_response_bytes=config.max_response_bytes,
            )
        except MetaFlowActionError as exc:
            return _encrypted_error(
                exc,
                decrypted_request=decrypted_request,
                config=config,
                request_id=request_id,
                endpoint_hash=endpoint_hash,
                action=action,
            )
        except MetaFlowEndpointError as exc:
            return _encrypted_error(
                exc,
                decrypted_request=decrypted_request,
                config=config,
                request_id=request_id,
                endpoint_hash=endpoint_hash,
                action=action,
            )
        except Exception:
            return _encrypted_error(
                MetaFlowActionError(
                    "handler_failed",
                    "Flow action could not be completed.",
                    500,
                ),
                decrypted_request=decrypted_request,
                config=config,
                request_id=request_id,
                endpoint_hash=endpoint_hash,
                action=action,
            )

        _safe_log(
            logging.INFO,
            event="request_completed",
            request_id=request_id,
            endpoint_hash=endpoint_hash,
            status_code=200,
            action=action,
        )
        return _text_response(encrypted_response, 200)

    return blueprint


class _EndpointNotFound(LookupError):
    pass


def _resolve_config(
    endpoint_id: str,
    injected_resolver: ConfigResolver | None,
) -> MetaFlowEndpointConfig:
    registry = current_app.config.get("META_FLOW_DATA_EXCHANGE_ENDPOINTS")
    raw_config = registry.get(endpoint_id) if isinstance(registry, Mapping) else None
    resolver = injected_resolver or current_app.config.get(
        "META_FLOW_DATA_EXCHANGE_CONFIG_RESOLVER"
    )
    if raw_config is None and resolver is not None:
        if not callable(resolver):
            raise MetaFlowConfigurationError("endpoint_resolver_invalid")
        raw_config = resolver(endpoint_id)
    if raw_config is None:
        raise _EndpointNotFound()
    return coerce_endpoint_config(endpoint_id, raw_config)


def _application_payload_limit() -> int:
    configured = current_app.config.get(
        "META_FLOW_DATA_EXCHANGE_MAX_PAYLOAD_BYTES",
        DEFAULT_MAX_PAYLOAD_BYTES,
    )
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_PAYLOAD_BYTES
    return min(max(value, 1024), HARD_MAX_PAYLOAD_BYTES)


def _safe_action(decrypted_request: DecryptedMetaFlowRequest) -> str:
    action = normalize_action(decrypted_request.payload.get("action"))
    if action in {"ping", "init", "back", "data_exchange"}:
        data = decrypted_request.payload.get("data")
        if action != "ping" and isinstance(data, Mapping) and data.get("error") is not None:
            return "error"
        return action
    return "unsupported"


def _encrypted_error(
    error: MetaFlowEndpointError,
    *,
    decrypted_request: DecryptedMetaFlowRequest,
    config: MetaFlowEndpointConfig,
    request_id: str,
    endpoint_hash: str,
    action: str,
):
    try:
        encrypted = encrypt_flow_response(
            build_encrypted_error_payload(error, request_id),
            decrypted_request,
            max_response_bytes=config.max_response_bytes,
        )
    except Exception:
        fallback = MetaFlowResponseError()
        return _json_error(
            fallback,
            request_id=request_id,
            endpoint_hash=endpoint_hash,
            action=action,
        )
    _safe_log(
        logging.WARNING if error.status_code < 500 else logging.ERROR,
        event=error.code,
        request_id=request_id,
        endpoint_hash=endpoint_hash,
        status_code=error.status_code,
        action=action,
    )
    return _text_response(encrypted, error.status_code)


def _json_error(
    error: MetaFlowEndpointError,
    *,
    request_id: str,
    endpoint_hash: str,
    action: str = "unknown",
):
    _safe_log(
        logging.WARNING if error.status_code < 500 else logging.ERROR,
        event=error.code,
        request_id=request_id,
        endpoint_hash=endpoint_hash,
        status_code=error.status_code,
        action=action,
    )
    response = jsonify(
        {
            "error": {
                "code": error.code,
                "message": error.safe_message,
                "request_id": request_id,
            }
        }
    )
    response.status_code = error.status_code
    return _secure_headers(response)


def _text_response(body: str, status_code: int) -> Response:
    response = Response(body, status=status_code, content_type="text/plain; charset=utf-8")
    return _secure_headers(response)


def _secure_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _safe_log(
    level: int,
    *,
    event: str,
    request_id: str,
    endpoint_hash: str,
    status_code: int,
    action: str = "unknown",
) -> None:
    safe_event = event if _LOG_LABEL.fullmatch(event) else "invalid_event_label"
    safe_action = action if _LOG_LABEL.fullmatch(action) else "unknown"
    current_app.logger.log(
        level,
        (
            "meta_flow_data_exchange event=%s request_id=%s "
            "endpoint_hash=%s action=%s status_code=%s"
        ),
        safe_event,
        request_id,
        endpoint_hash,
        safe_action,
        status_code,
    )


meta_flow_data_exchange_bp = create_meta_flow_data_exchange_blueprint()
