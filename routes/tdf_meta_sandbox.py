"""A separate Meta test-number callback; never aliases the Twilio webhook."""
from flask import Blueprint, current_app, jsonify, request
from models import db
from services import tdf_meta_sandbox as pilot
from services.meta_whatsapp_cloud import MetaContractError
from services.meta_whatsapp_webhook import MAX_BODY_BYTES, verify_challenge
from services.tenant_provider_credentials import ProviderCredentialError

tdf_meta_sandbox_bp = Blueprint("tdf_meta_sandbox", __name__)
CHALLENGE_KEYS = frozenset({"hub.mode", "hub.verify_token", "hub.challenge"})
MAX_CHALLENGE_QUERY_BYTES = 4096
MAX_CHALLENGE_PARAMETERS = 16
MAX_CHALLENGE_KEY_BYTES = 128


def _challenge_query():
    """Meta may add inert GET parameters; authentication fields stay unique.

    Diagnostics contain only fixed field names and counts. Extra names are also
    free-form input, so neither their names nor any values enter logs.
    """
    size = len(request.query_string)
    if size > MAX_CHALLENGE_QUERY_BYTES:
        current_app.logger.info("Meta TDF challenge GET rejected reason=query_size query_bytes=%d", size)
        raise pilot.PilotError("tdf_meta_challenge_invalid", 400)
    pairs = list(request.args.items(multi=True))
    present = sorted(CHALLENGE_KEYS.intersection(request.args))
    count = len(pairs)
    duplicate_required = sum(len(request.args.getlist(key)) > 1 for key in CHALLENGE_KEYS)
    current_app.logger.info(
        "Meta TDF challenge GET fields=%s parameter_count=%d extra_count=%d duplicate_required_count=%d query_bytes=%d",
        present, count, sum(key not in CHALLENGE_KEYS for key, _ in pairs), duplicate_required, size)
    if (count > MAX_CHALLENGE_PARAMETERS
            or any(not key or len(key.encode("utf-8")) > MAX_CHALLENGE_KEY_BYTES for key, _ in pairs)
            or not CHALLENGE_KEYS.issubset(request.args)
            or any(len(request.args.getlist(key)) != 1 for key in CHALLENGE_KEYS)):
        raise pilot.PilotError("tdf_meta_challenge_invalid", 400)
    return {key: request.args[key] for key in CHALLENGE_KEYS}


@tdf_meta_sandbox_bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    return response


@tdf_meta_sandbox_bp.route("/webhook/meta/tdf-sandbox", methods=["GET", "POST"])
def callback():
    try:
        cfg = pilot.settings(current_app.config)
        if request.method == "GET":
            query = _challenge_query()
            result = verify_challenge(mode=query["hub.mode"], token=query["hub.verify_token"],
                challenge=query["hub.challenge"], verify_token=cfg["VERIFY_TOKEN"])
            return result, 200, {"Content-Type": "text/plain; charset=utf-8"}
        if request.args or request.mimetype != "application/json":
            raise pilot.PilotError("tdf_meta_json_required", 400)
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            raise pilot.PilotError("tdf_meta_body_too_large", 413)
        raw = request.stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise pilot.PilotError("tdf_meta_body_too_large", 413)
        result = pilot.process(raw, request.headers.get("X-Hub-Signature-256", ""))
        # Acknowledges persisted receipts, never implies real delivery.
        return jsonify(result), 200
    except pilot.PilotError as exc:
        db.session.rollback()
        return jsonify(reason_code=exc.code), exc.status
    except (MetaContractError, ProviderCredentialError):
        db.session.rollback()
        return jsonify(reason_code="tdf_meta_authentication_or_binding_denied"), 403
    except Exception:
        db.session.rollback()
        return jsonify(reason_code="tdf_meta_runtime_unavailable"), 503
