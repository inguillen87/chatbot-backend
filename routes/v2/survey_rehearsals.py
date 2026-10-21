"""Explicit production rehearsal surface; never an official survey alias."""
import json
from functools import wraps

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import SQLAlchemyError

from database import db
from config import ALLOWED_ORIGINS, is_same_site_credential_origin
from services import production_survey_rehearsal as rehearsal
from services.encuestas_service import EncuestaError, resolve_survey_submission_id
from services.public_survey_intake import _consume_rate_limit, public_survey_client_ip
from utils.auth_helpers import auth_sin_escrituras_implicitas, token_requerido, user_from_token
from utils.roles import is_authorized_superadmin_user
from utils.tenant_admin_access import can_manage_tenant_control_plane

bp = Blueprint("production_survey_rehearsals", __name__, url_prefix="/api/v2")


def _response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _scope(slug):
    hints = [request.headers.get("X-Tenant-Slug"), request.headers.get("X-Tenant"),
             request.args.get("tenant_slug"), request.args.get("tenant")]
    if any(value is not None and value != slug for value in hints):
        raise rehearsal.RehearsalError("rehearsal_tenant_selector_conflict", 400)
    ids = [request.headers.get("X-Tenant-Id"), request.args.get("tenant_id")]
    if any(value is not None for value in ids):
        tenant = rehearsal._tenant(slug)
        if any(value is not None and value != str(tenant.id) for value in ids):
            raise rehearsal.RehearsalError("rehearsal_tenant_selector_conflict", 400)


def _panel_auth_required(view):
    """Reject implicit credentials before the shared legacy token extractor."""
    authenticated = token_requerido(view)

    @wraps(view)
    def protected(*args, **kwargs):
        if (request.method != "OPTIONS" and not request.headers.get("Authorization")
            and not request.cookies.get(current_app.config.get("AUTH_TOKEN_COOKIE_NAME") or "auth_token")):
            return _response(rehearsal.RehearsalError("rehearsal_auth_session_required", 401).to_dict(), 401)
        return authenticated(*args, **kwargs)
    return protected


def _current_panel_session(user):
    """Accept the official panel transports, with current durable authority.

    Clerk's exchange issues tenant_owner, usually in an HttpOnly cookie.
    Query/body/entity credentials are not a rehearsal authentication transport.
    """
    claims = getattr(g, "token_payload", {})
    authorization = request.headers.get("Authorization")
    token = getattr(g, "auth_token", None)
    if authorization is not None:
        parts = authorization.split()
        valid_transport = len(parts) == 2 and parts[0].lower() == "bearer" and parts[1] == token
        cookie_transport = False
    else:
        cookie = request.cookies.get(current_app.config.get("AUTH_TOKEN_COOKIE_NAME") or "auth_token")
        valid_transport = bool(cookie and token == cookie)
        cookie_transport = True
    provider, audience = claims.get("auth_provider"), claims.get("auth_audience")
    valid_audience = (
        provider == "native" and audience == "panel"
        or provider == "clerk" and (
            audience == "panel"
            or audience == "tenant_owner" and claims.get("auth_intent") == "tenant_owner"
        )
    )
    if (not valid_transport or not valid_audience
        or claims.get("user_id") != user.id
        or claims.get("session_kind") in {"widget", "demo"} or claims.get("demo_mode")
        or any(key in request.args for key in ("token", "entityToken", "entity_token", "empresa_token"))):
        raise rehearsal.RehearsalError("rehearsal_auth_session_required", 401)
    # The native Flask branch can carry a different same-actor JWT cookie.
    # Validate this exact JWT's signature/expiry and durable/provider lineage,
    # then bind all service/commit rechecks to it rather than an ambient cookie.
    credential_actor = user_from_token(token)
    if credential_actor is None or credential_actor.id != user.id:
        raise rehearsal.RehearsalError("rehearsal_auth_session_required", 401)
    g.auth_credential_source = "token"
    actor = rehearsal._actor(user.id)
    if cookie_transport and request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("Origin")
        allowed = current_app.config.get("CORS_CREDENTIALS_ALLOWED_ORIGINS") or ALLOWED_ORIGINS
        if (not origin or not any(isinstance(value, str) and origin == value.rstrip("/") for value in allowed)
            or not is_same_site_credential_origin(origin, backend_url=current_app.config.get("BACKEND_URL"),
                                                  public_root_domain=current_app.config.get("PUBLIC_ROOT_DOMAIN"))):
            raise rehearsal.RehearsalError("rehearsal_origin_forbidden", 403)
        if request.mimetype != "application/json":
            raise rehearsal.RehearsalError("rehearsal_invalid_payload", 400)
    return actor


def _payload(*, create=False):
    if request.content_length is not None and request.content_length > rehearsal.MAX_BODY_BYTES:
        raise rehearsal.RehearsalError("rehearsal_payload_too_large", 413)
    # Existing public tenant middleware may already have cached this small
    # body. Validate those exact bytes instead of rereading an exhausted stream.
    cached = getattr(request, "_cached_data", None)
    raw = cached if isinstance(cached, bytes) else request.stream.read(rehearsal.MAX_BODY_BYTES + 1)
    if len(raw) > rehearsal.MAX_BODY_BYTES:
        raise rehearsal.RehearsalError("rehearsal_payload_too_large", 413)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        raise rehearsal.RehearsalError("rehearsal_invalid_payload", 400) from None
    valid = isinstance(payload, dict) and (
        not payload if create else set(payload) == {"submission_id", "option_id"}
        and isinstance(payload.get("option_id"), str) and payload["option_id"] in {"yes", "no"}
    )
    if not valid:
        raise rehearsal.RehearsalError("rehearsal_invalid_payload", 400)
    key = resolve_survey_submission_id(payload, header_value=request.headers.get("Idempotency-Key"), required=True)
    return payload, key


def _handled(action):
    try:
        return action()
    except rehearsal.RehearsalError as exc:
        db.session.rollback()
        return _response(exc.to_dict(), exc.status_code)
    except EncuestaError as exc:
        db.session.rollback()
        return _response({"contract_version": rehearsal.CONTRACT, **(exc.payload or {})}, exc.status_code)
    except SQLAlchemyError:
        error = rehearsal.storage_error()
        return _response(error.to_dict(), error.status_code)


@bp.route("/tenants/<string:tenant_slug>/survey-rehearsals", methods=["GET"])
@_panel_auth_required
@auth_sin_escrituras_implicitas
def list_runs(current_user, tenant_slug):
    def action():
        _current_panel_session(current_user)
        _scope(tenant_slug)
        tenant = rehearsal._tenant(tenant_slug)
        if not can_manage_tenant_control_plane(current_user, tenant):
            raise rehearsal.RehearsalError("rehearsal_tenant_scope_forbidden", 403)
        return _response(rehearsal.list_rehearsals(tenant_slug, current_user))
    return _handled(action)


@bp.route("/tenants/<string:tenant_slug>/survey-rehearsals", methods=["POST"])
@_panel_auth_required
@auth_sin_escrituras_implicitas
def create_run(current_user, tenant_slug):
    try:
        _current_panel_session(current_user)
    except rehearsal.RehearsalError as exc:
        return _response(exc.to_dict(), exc.status_code)
    if not is_authorized_superadmin_user(current_user):
        return _response(rehearsal.RehearsalError("rehearsal_superadmin_required", 403).to_dict(), 403)
    def action():
        _scope(tenant_slug)
        _, key = _payload(create=True)
        result = rehearsal.create_rehearsal(tenant_slug, current_user, key)
        return _response(result, 200 if result["replayed"] else 201)
    return _handled(action)


@bp.route("/tenants/<string:tenant_slug>/survey-rehearsals/status", methods=["GET"])
@_panel_auth_required
@auth_sin_escrituras_implicitas
def create_status(current_user, tenant_slug):
    def action():
        _current_panel_session(current_user)
        _scope(tenant_slug)
        return _response(rehearsal.find_created_rehearsal(tenant_slug, current_user, request.args.get("submission_id")))
    return _handled(action)


@bp.route("/public/tenants/<string:tenant_slug>/survey-rehearsals/<string:run_id>", methods=["GET"])
@bp.route("/public/tenants/<string:tenant_slug>/survey-rehearsals/<string:run_id>/results", methods=["GET"])
def public_run(tenant_slug, run_id):
    def action():
        _scope(tenant_slug)
        return _response(rehearsal.read_rehearsal(tenant_slug, run_id))
    return _handled(action)


@bp.route("/public/tenants/<string:tenant_slug>/survey-rehearsals/<string:run_id>/respond", methods=["POST"])
@_panel_auth_required
@auth_sin_escrituras_implicitas
def respond(current_user, tenant_slug, run_id):
    def action():
        _current_panel_session(current_user)
        _scope(tenant_slug)
        payload, key = _payload()
        result = rehearsal.submit_rehearsal(tenant_slug, run_id, current_user, payload, key,
                    rate_check=lambda token: _consume_rate_limit(token, client_ip=public_survey_client_ip()))
        return _response(result, 200 if result["replayed"] else 201)
    return _handled(action)


@bp.route("/public/tenants/<string:tenant_slug>/survey-rehearsals/<string:run_id>/respond/status", methods=["GET"])
@_panel_auth_required
@auth_sin_escrituras_implicitas
def submission_status(current_user, tenant_slug, run_id):
    def action():
        _current_panel_session(current_user)
        _scope(tenant_slug)
        return _response(rehearsal.find_rehearsal_submission(tenant_slug, run_id, current_user,
                                                            request.args.get("submission_id")))
    return _handled(action)
