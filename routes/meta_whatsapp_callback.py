"""Meta direct webhook admission, independent of the Twilio and TDF routes."""

from collections.abc import Mapping
import time

from flask import Blueprint, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge

from database import db
from services.meta_whatsapp_batch_admission import (
    MetaBatchAdmissionError, meta_callback_recipient_scope, stage_meta_webhook_batch,
)
from services.meta_whatsapp_cloud import MetaContractError
from services.meta_whatsapp_credentials import meta_id
from services.meta_whatsapp_outbox import RUNTIME_EXTENSION
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, MetaOutboundRuntime
from services.meta_whatsapp_webhook import MAX_BODY_BYTES, verify_challenge, verify_signature
from services.outbox_execution_budget import (
    activate_outbox_execution_budget,
    install_outbox_database_timeout_hook,
    require_outbox_time_remaining,
)
from services.tenant_provider_credentials import ProviderCredentialError


meta_whatsapp_callback_bp = Blueprint("meta_whatsapp_callback", __name__)
_VERIFY_TOKENS_KEY = "META_WHATSAPP_CALLBACK_VERIFY_TOKENS"
_CHALLENGE_KEYS = ("hub.mode", "hub.verify_token", "hub.challenge")
_CALLBACK_DEADLINE_SECONDS = 16


def _credentials(app_id):
    config = current_app.config
    if config.get("META_WHATSAPP_CALLBACK_ENABLED") is not True:
        return None
    try:
        meta_id(app_id)
        meta_callback_recipient_scope(config, app_id)
    except (MetaBatchAdmissionError, ProviderCredentialError):
        return None
    secrets = config.get(APP_SECRETS_KEY)
    tokens = config.get(_VERIFY_TOKENS_KEY)
    secret = secrets.get(app_id) if isinstance(secrets, Mapping) else None
    token = tokens.get(app_id) if isinstance(tokens, Mapping) else None
    if (not isinstance(secret, str) or not 16 <= len(secret) <= 4096
            or not isinstance(token, str) or not 16 <= len(token) <= 256):
        return None
    return secret, token


def _error(code, status):
    return jsonify(reason_code=code), status


def _challenge(token):
    if len(request.query_string) > 4096:
        return _error("meta_callback_challenge_invalid", 400)
    args = request.args
    if (sum(len(values) for _, values in args.lists()) > 16
            or any(len(args.getlist(key)) != 1 for key in _CHALLENGE_KEYS)):
        return _error("meta_callback_challenge_invalid", 400)
    try:
        challenge = verify_challenge(mode=args["hub.mode"], token=args["hub.verify_token"],
                                     challenge=args["hub.challenge"], verify_token=token)
    except MetaContractError:
        return _error("meta_callback_challenge_denied", 403)
    return challenge, 200, {"Content-Type": "text/plain; charset=utf-8"}


@meta_whatsapp_callback_bp.after_request
def no_store(response):
    response.headers["Cache-Control"] = "private, no-store"
    return response


@meta_whatsapp_callback_bp.route("/webhook/meta/whatsapp/<app_id>", methods=["GET", "POST"],
                                provide_automatic_options=False)
def callback(app_id):
    credentials = _credentials(app_id)
    if credentials is None:
        return _error("meta_callback_unavailable", 404)
    secret, token = credentials
    if request.method in {"GET", "HEAD"}:
        return _challenge(token)

    if (request.query_string or request.mimetype != "application/json"
            or request.headers.get("Content-Encoding", "identity").lower() != "identity"):
        return _error("meta_callback_json_required", 400)
    try:
        if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
            raise RequestEntityTooLarge
        raw = request.stream.read(MAX_BODY_BYTES + 1)
        if len(raw) > MAX_BODY_BYTES:
            raise RequestEntityTooLarge
    except RequestEntityTooLarge:
        return _error("meta_callback_body_too_large", 413)
    except BadRequest:
        return _error("meta_callback_body_invalid", 400)

    signature = request.headers.get("X-Hub-Signature-256", "")
    try:
        verify_signature(raw_body=raw, signature=signature, app_secret=secret)
    except MetaContractError:
        return _error("meta_callback_signature_denied", 403)

    try:
        runtime = current_app.extensions.get(RUNTIME_EXTENSION)
        if runtime is None:
            runtime = MetaOutboundRuntime.from_app()
        with activate_outbox_execution_budget(
            deadline_monotonic=time.monotonic() + _CALLBACK_DEADLINE_SECONDS,
            cleanup_reserve_seconds=2,
        ), current_app.app_context():
            # Own a fresh scoped session, separate from request/tenant middleware.
            install_outbox_database_timeout_hook(db.engine)
            session = db.session()
            with session.begin() as transaction:
                connection = session.connection()
                if connection.dialect.name == "sqlite" and not connection.connection.driver_connection.in_transaction:
                    connection.exec_driver_sql("BEGIN")
                stage_meta_webhook_batch(session, transaction, raw_body=raw, signature=signature,
                                         app_id=app_id, runtime=runtime)
                require_outbox_time_remaining()
    except Exception:
        # Unknown/unadmitted events and uncertain commits both stay unacknowledged.
        # Never expose an identifier, upstream exception, raw event or credential.
        current_app.logger.warning("Meta WhatsApp callback batch not acknowledged")
        return _error("meta_callback_batch_held", 503)
    return "", 200
