from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import RequestEntityTooLarge

from services.omnichannel_adapter_security import (
    CONTRACT_VERSION,
    OmnichannelAdapterError,
    adapter_payload_limit,
    claim_verified_adapter_delivery,
    normalize_adapter_event,
    parse_adapter_json,
    verify_adapter_request,
)
from services.omnichannel_service import registrar_interaccion_omnicanal
from services.ticket_service import (
    TicketIdempotencyConflict,
    TicketIdempotencyValidationError,
)
from services.webhook_delivery_service import (
    CLAIMED,
    DUPLICATE,
    IN_PROGRESS,
    WebhookDeliveryValidationError,
    fail_delivery,
)

omnichannel_bp = Blueprint("omnichannel_bp", __name__)
logger = logging.getLogger(__name__)


def _error_response(error: OmnichannelAdapterError):
    return (
        jsonify(
            {
                "contract_version": CONTRACT_VERSION,
                "error": {"code": error.code, "message": error.message},
                "retryable": error.retryable,
            }
        ),
        error.status_code,
    )


@omnichannel_bp.route("/omnichannel/inbound", methods=["POST"])
def inbound_interaction():
    """Reject the legacy unsigned catch-all webhook.

    Provider webhooks must enter through a channel-specific adapter that
    validates the provider signature and derives the tenant from a verified
    receiving endpoint or credential. The previous implementation trusted
    tenant and contact selectors supplied in the request body, so it is unsafe
    to expose publicly. Dedicated WhatsApp and voice webhooks are unaffected.
    """

    return (
        jsonify(
            {
                "contract_version": "omnichannel.generic_inbound_disabled.v1",
                "error": {
                    "code": "generic_omnichannel_inbound_disabled",
                    "message": "Usá un adaptador de entrada autenticado para el proveedor.",
                },
                "retryable": False,
            }
        ),
        404,
    )


@omnichannel_bp.route(
    "/omnichannel/adapters/<int:connection_id>/inbound",
    methods=["POST"],
)
def signed_adapter_inbound(connection_id: int):
    """Accept one normalized event from a tenant-bound signed adapter.

    This endpoint never accepts tenant selectors from the request body.  The
    tenant, provider and channel are derived from the verified
    ``ProviderConnection`` in the URL.  The durable webhook claim is fenced
    and completed by the same database transaction that appends the event to
    an existing ticket.  New-ticket retries use the ticket service's own
    tenant-scoped idempotency receipt.
    """

    if request.mimetype != "application/json":
        return _error_response(
            OmnichannelAdapterError(
                "adapter_json_required",
                "El adaptador debe enviar application/json.",
                status_code=415,
            )
        )

    delivery_claim = None
    try:
        # Flask 3.1 enforces this limit while reading both Content-Length and
        # chunked requests. Set it before ``get_data`` so an unauthenticated
        # sender cannot force an unbounded in-memory body before signature
        # verification applies its own defense-in-depth length check.
        request.max_content_length = adapter_payload_limit(current_app.config)
        raw_body = request.get_data(cache=True, as_text=False)
        verified = verify_adapter_request(
            config=current_app.config,
            connection_id=connection_id,
            headers=request.headers,
            raw_body=raw_body,
        )
        decoded = parse_adapter_json(raw_body)
        normalized = normalize_adapter_event(decoded, verified)
        delivery_claim = claim_verified_adapter_delivery(verified, raw_body)
        if (
            delivery_claim.payload_digest != verified.payload_digest
            or delivery_claim.event_type != verified.event_type
        ):
            return _error_response(
                OmnichannelAdapterError(
                    "adapter_event_payload_conflict",
                    "El identificador del evento ya está asociado a otro contenido.",
                    status_code=409,
                )
            )
        if delivery_claim.outcome == DUPLICATE:
            return (
                jsonify(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "status": "duplicate",
                        "event_id": verified.event_id,
                        "retryable": False,
                    }
                ),
                200,
            )
        if delivery_claim.outcome == IN_PROGRESS:
            return (
                jsonify(
                    {
                        "contract_version": CONTRACT_VERSION,
                        "status": "in_progress",
                        "event_id": verified.event_id,
                        "retryable": True,
                    }
                ),
                202,
            )
        if delivery_claim.outcome != CLAIMED or not delivery_claim.should_process:
            return _error_response(
                OmnichannelAdapterError(
                    "invalid_adapter_delivery_state",
                    "No pudimos reclamar el evento de forma segura.",
                    status_code=503,
                    retryable=True,
                )
            )

        result = registrar_interaccion_omnicanal(
            normalized.ticket_payload,
            delivery_claim=delivery_claim,
        )
        if not result.get("exito"):
            reason = str(result.get("motivo") or "omnichannel_persistence_failed")
            fail_delivery(
                delivery_claim.delivery_id,
                delivery_claim.attempts,
                reason,
            )
            retryable = reason in {"db_error", "delivery_claim_lost"}
            return _error_response(
                OmnichannelAdapterError(
                    reason,
                    (
                        "No pudimos persistir el evento en este momento."
                        if retryable
                        else "El evento no cumple el contrato de ingreso."
                    ),
                    status_code=503 if retryable else 422,
                    retryable=retryable,
                )
            )

        return (
            jsonify(
                {
                    "contract_version": CONTRACT_VERSION,
                    "status": "accepted",
                    "event_id": verified.event_id,
                    "ticket": {
                        "id": result.get("ticket_id"),
                        "created": bool(result.get("nuevo_ticket")),
                        "source_model": result.get("source_model"),
                    },
                    "message": {
                        "kind": normalized.message_kind,
                        "has_media": normalized.has_media,
                        "media_processing": (
                            "pending"
                            if normalized.awaiting_media_processing
                            else "not_required"
                        ),
                    },
                    "retryable": False,
                }
            ),
            201 if result.get("nuevo_ticket") else 200,
        )
    except RequestEntityTooLarge:
        return _error_response(
            OmnichannelAdapterError(
                "adapter_payload_too_large",
                "El evento supera el tamaño permitido.",
                status_code=413,
                retryable=False,
            )
        )
    except OmnichannelAdapterError as exc:
        return _error_response(exc)
    except (TicketIdempotencyConflict, TicketIdempotencyValidationError) as exc:
        if delivery_claim is not None and delivery_claim.should_process:
            fail_delivery(
                delivery_claim.delivery_id,
                delivery_claim.attempts,
                exc,
            )
        is_conflict = isinstance(exc, TicketIdempotencyConflict)
        return _error_response(
            OmnichannelAdapterError(
                (
                    "adapter_event_payload_conflict"
                    if is_conflict
                    else "invalid_adapter_idempotency"
                ),
                (
                    "El identificador del evento ya está asociado a otro contenido."
                    if is_conflict
                    else "La identidad idempotente del evento no es válida."
                ),
                status_code=409 if is_conflict else 422,
                retryable=False,
            )
        )
    except WebhookDeliveryValidationError:
        return _error_response(
            OmnichannelAdapterError(
                "invalid_adapter_event_identity",
                "La identidad del evento no es válida.",
                status_code=400,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive production boundary
        if delivery_claim is not None and delivery_claim.should_process:
            try:
                fail_delivery(
                    delivery_claim.delivery_id,
                    delivery_claim.attempts,
                    exc,
                )
            except Exception:
                logger.exception("Could not fail omnichannel delivery claim")
        logger.exception(
            "Signed omnichannel adapter failed connection_id=%s error_type=%s",
            connection_id,
            type(exc).__name__,
        )
        return _error_response(
            OmnichannelAdapterError(
                "omnichannel_adapter_internal_error",
                "No pudimos procesar el evento en este momento.",
                status_code=503,
                retryable=True,
            )
        )
