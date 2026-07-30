from __future__ import annotations

from flask import Blueprint, jsonify

omnichannel_bp = Blueprint("omnichannel_bp", __name__)


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
