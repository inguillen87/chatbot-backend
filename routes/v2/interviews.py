"""Fail-closed v2 API for assessment programs and interview evidence.

This API deliberately ends at ``awaiting_human_review``. It has no endpoint
for admission, rejection, hiring, eligibility, scoring, or final decisions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
import uuid

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy.exc import IntegrityError

from extensions import db
from routes.v2.tenants import V2TenantResolutionError, resolve_tenant_v2
from services.interview_access_policy import (
    INTERVIEW_ASSIGNMENTS_MANAGE,
    INTERVIEW_CASES_CREATE,
    INTERVIEW_CASES_READ,
    INTERVIEW_EVIDENCE_WRITE,
    INTERVIEW_PROGRAMS_MANAGE,
    INTERVIEW_SESSIONS_CONDUCT,
    INTERVIEW_SESSIONS_CREATE,
    missing_interview_capabilities,
)
from services.interview_service import (
    InterviewDomainError,
    InterviewMutation,
    assign_interview_session,
    build_interview_assignment_candidates,
    build_interview_inbox,
    complete_interview_session,
    create_assessment_case,
    create_interview_evidence,
    create_interview_session,
    create_program,
    create_program_version,
    get_assessment_case,
    get_interview_session,
    issue_interview_consent_challenge,
    publish_program_version,
    register_interview_consent_presentation,
    serialize_assessment_case,
    serialize_consent_challenge,
    serialize_consent_presentation,
    serialize_evidence,
    serialize_interview_session,
    serialize_interview_resume,
    serialize_interview_assignment,
    serialize_program,
    start_interview_session,
    validate_idempotency_key,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.auth_helpers import token_requerido
from utils.roles import normalize_tenant_slug


v2_interviews_bp = Blueprint(
    "v2_interviews",
    __name__,
    url_prefix="/api/v2/interviews",
)

_FEATURE_FLAG = "ENABLE_ASSESSMENT_INTERVIEWS_V1"
_ASSIGNMENT_FEATURE_FLAG = "ENABLE_INTERVIEW_ASSIGNMENTS_V1"


@v2_interviews_bp.after_request
def _interview_responses_are_never_cached(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _request_id() -> str:
    incoming = (
        request.headers.get("X-Request-Id")
        or request.headers.get("X-Correlation-Id")
        or getattr(g, "request_id", None)
        or ""
    )
    request_id = str(incoming).strip() or uuid.uuid4().hex
    g.request_id = request_id
    return request_id


def _json_response(payload: dict[str, Any], status_code: int = 200):
    request_id = _request_id()
    body = dict(payload)
    body.setdefault("request_id", request_id)
    response = jsonify(body)
    response.status_code = status_code
    response.headers["X-Request-Id"] = request_id
    return response


def _error_response(
    message: str,
    status_code: int,
    reason_code: str,
    action_hint: str,
    *,
    retryable: bool = False,
    **extra: Any,
):
    return _json_response(
        {
            "contract_version": "shared.error.v1",
            "status_code": status_code,
            "reason_code": reason_code,
            "retryable": retryable,
            "action_hint": action_hint,
            "error": {"code": status_code, "message": message},
            "message": message,
            **extra,
        },
        status_code,
    )


def _domain_error_response(exc: InterviewDomainError):
    return _error_response(
        exc.message,
        exc.status_code,
        exc.reason_code,
        exc.action_hint,
        retryable=exc.retryable,
        **exc.details,
    )


def _json_object(*, default_empty: bool = False) -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if payload is None and default_empty:
        return {}
    if not isinstance(payload, dict):
        raise InterviewDomainError(
            "Request body must be a JSON object",
            status_code=400,
            reason_code="interview_json_object_required",
            action_hint="send_json_object",
        )
    return payload


def _authorized_tenant(current_user, *required_capabilities: str):
    if current_app.config.get(_FEATURE_FLAG) is not True:
        return None, _error_response(
            "Assessment interviews are not enabled for this deployment",
            403,
            "assessment_interviews_feature_disabled",
            "enable_after_security_review",
        )

    explicit_slug = str(request.headers.get("X-Tenant-Slug") or "").strip()
    if not explicit_slug:
        return None, _error_response(
            "X-Tenant-Slug is required",
            400,
            "interview_tenant_required",
            "send_tenant_slug",
        )
    requested_slug = normalize_tenant_slug(explicit_slug)
    try:
        tenant = resolve_tenant_v2(required=True, explicit_slug=explicit_slug)
    except V2TenantResolutionError as exc:
        return None, _error_response(
            exc.message,
            exc.status_code,
            "interview_tenant_resolution_failed",
            "check_tenant_slug",
        )

    # resolve_tenant_v2 accepts several context sources. This endpoint requires
    # the explicitly selected tenant to win, preventing JWT/header confusion.
    if not tenant or normalize_tenant_slug(tenant.slug) != requested_slug:
        return None, _error_response(
            "Tenant not found",
            404,
            "interview_tenant_resolution_failed",
            "check_tenant_slug",
        )
    if not _is_authorized_for_tenant(
        current_user,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
    ):
        return None, _error_response(
            "Insufficient permissions for this tenant",
            403,
            "interview_tenant_forbidden",
            "switch_tenant",
        )

    missing = missing_interview_capabilities(
        current_user,
        tenant,
        *required_capabilities,
    )
    if missing:
        return None, _error_response(
            "A tenant interview capability is required",
            403,
            "interview_capability_required",
            "request_capability_from_tenant_admin",
            required_capabilities=list(required_capabilities),
            missing_capabilities=missing,
        )
    g.tenant_profile = tenant
    return tenant, None


def _execute_mutation(
    operation: Callable[[], InterviewMutation],
    serializer: Callable[[Any], dict[str, Any]],
    resource_name: str,
    *,
    created_status: int = 201,
    contract_version: str = "assessment.interviews.api.v1",
):
    try:
        mutation = operation()
        if not mutation.replayed:
            db.session.commit()
    except InterviewDomainError as exc:
        db.session.rollback()
        return _domain_error_response(exc)
    except IntegrityError:
        db.session.rollback()
        return _error_response(
            "A concurrent request used the same operation identity",
            409,
            "interview_concurrent_write_conflict",
            "retry_same_request",
            retryable=True,
        )
    except Exception as exc:  # Audit and domain persistence must fail together.
        db.session.rollback()
        current_app.logger.error(
            "interview mutation rolled back request_id=%s path=%s error_type=%s",
            _request_id(),
            request.path,
            type(exc).__name__,
        )
        return _error_response(
            "The interview mutation could not be audited and was rolled back",
            503,
            "interview_audit_commit_failed",
            "retry_later",
            retryable=True,
        )

    payload = {
        "ok": True,
        "contract_version": contract_version,
        resource_name: serializer(mutation.value),
        "idempotency_replayed": mutation.replayed,
    }
    response = _json_response(
        payload,
        200 if mutation.replayed else created_status,
    )
    response.headers["Idempotency-Replayed"] = (
        "true" if mutation.replayed else "false"
    )
    return response


def _idempotency_key() -> str:
    return validate_idempotency_key(request.headers.get("Idempotency-Key"))


def _authorized_assignment_tenant(current_user):
    tenant, error = _authorized_tenant(current_user)
    if error:
        return None, error
    if current_app.config.get(_ASSIGNMENT_FEATURE_FLAG) is not True:
        return None, _error_response(
            "Managed interview assignments are not enabled for this deployment",
            403,
            "interview_assignments_feature_disabled",
            "enable_after_assignment_security_review",
        )
    missing = missing_interview_capabilities(
        current_user,
        tenant,
        INTERVIEW_ASSIGNMENTS_MANAGE,
    )
    if missing:
        return None, _error_response(
            "A tenant interview assignment capability is required",
            403,
            "interview_assignment_capability_required",
            "request_capability_from_tenant_admin",
            required_capabilities=[INTERVIEW_ASSIGNMENTS_MANAGE],
            missing_capabilities=missing,
        )
    return tenant, None


@v2_interviews_bp.route("/inbox", methods=["GET"])
@token_requerido
def list_interview_inbox_v2(current_user):
    """Return the bounded operational inbox without inventing review writes."""

    tenant, error = _authorized_tenant(current_user, INTERVIEW_CASES_READ)
    if error:
        return error
    raw_limit = str(request.args.get("limit") or "50").strip()
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 0
    can_view_resume = not missing_interview_capabilities(
        current_user,
        tenant,
        INTERVIEW_SESSIONS_CONDUCT,
    )
    assignment_feature_enabled = (
        current_app.config.get(_ASSIGNMENT_FEATURE_FLAG) is True
    )
    can_assign = bool(
        assignment_feature_enabled
        and not missing_interview_capabilities(
            current_user,
            tenant,
            INTERVIEW_ASSIGNMENTS_MANAGE,
        )
    )
    try:
        inbox = build_interview_inbox(
            tenant,
            can_view_resume=can_view_resume,
            assignment_feature_enabled=assignment_feature_enabled,
            can_assign=can_assign,
            limit=limit,
            status=request.args.get("status"),
        )
    except InterviewDomainError as exc:
        return _domain_error_response(exc)
    response = _json_response(
        {
            "ok": True,
            "contract_version": "assessment.interviews.api.v1",
            "inbox": inbox,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@v2_interviews_bp.route("/assignment-candidates", methods=["GET"])
@token_requerido
def list_interview_assignment_candidates_v2(current_user):
    tenant, error = _authorized_assignment_tenant(current_user)
    if error:
        return error
    return _json_response(
        {
            "ok": True,
            "contract_version": "assessment.interviews.api.v1",
            "assignment_candidates": build_interview_assignment_candidates(tenant),
        }
    )


@v2_interviews_bp.route("/programs", methods=["POST"])
@token_requerido
def create_program_v2(current_user):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_PROGRAMS_MANAGE)
    if error:
        return error
    return _execute_mutation(
        lambda: create_program(
            tenant,
            current_user,
            _json_object(),
            _idempotency_key(),
        ),
        lambda value: serialize_program(value[0], value[1]),
        "program",
    )


@v2_interviews_bp.route("/programs/<int:program_id>/versions", methods=["POST"])
@token_requerido
def create_program_version_v2(current_user, program_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_PROGRAMS_MANAGE)
    if error:
        return error
    return _execute_mutation(
        lambda: create_program_version(
            tenant,
            current_user,
            program_id,
            _json_object(),
            _idempotency_key(),
        ),
        lambda value: serialize_program(value[0], value[1]),
        "program",
    )


@v2_interviews_bp.route(
    "/programs/<int:program_id>/versions/<int:version_number>/publish",
    methods=["POST"],
)
@token_requerido
def publish_program_version_v2(current_user, program_id: int, version_number: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_PROGRAMS_MANAGE)
    if error:
        return error
    return _execute_mutation(
        lambda: publish_program_version(
            tenant,
            current_user,
            program_id,
            version_number,
            _json_object(default_empty=True),
            _idempotency_key(),
        ),
        lambda value: serialize_program(value[0], value[1]),
        "program",
        created_status=200,
    )


@v2_interviews_bp.route("/cases", methods=["POST"])
@token_requerido
def create_assessment_case_v2(current_user):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_CASES_CREATE)
    if error:
        return error
    return _execute_mutation(
        lambda: create_assessment_case(
            tenant,
            current_user,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_assessment_case,
        "case",
    )


@v2_interviews_bp.route("/cases/<int:case_id>", methods=["GET"])
@token_requerido
def get_assessment_case_v2(current_user, case_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_CASES_READ)
    if error:
        return error
    try:
        case = get_assessment_case(tenant, case_id)
    except InterviewDomainError as exc:
        return _domain_error_response(exc)
    return _json_response(
        {
            "ok": True,
            "contract_version": "assessment.interviews.api.v1",
            "case": serialize_assessment_case(case),
        }
    )


@v2_interviews_bp.route("/cases/<int:case_id>/sessions", methods=["POST"])
@token_requerido
def create_interview_session_v2(current_user, case_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CREATE)
    if error:
        return error
    return _execute_mutation(
        lambda: create_interview_session(
            tenant,
            current_user,
            case_id,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_interview_session,
        "session",
    )


@v2_interviews_bp.route(
    "/sessions/<int:session_id>/assignment",
    methods=["POST"],
)
@token_requerido
def assign_interview_session_v2(current_user, session_id: int):
    tenant, error = _authorized_assignment_tenant(current_user)
    if error:
        return error
    return _execute_mutation(
        lambda: assign_interview_session(
            tenant,
            current_user,
            session_id,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_interview_assignment,
        "assignment",
        contract_version="assessment.interviews.assignment.v1",
    )


@v2_interviews_bp.route(
    "/sessions/<int:session_id>/consent-challenges", methods=["POST"]
)
@token_requerido
def issue_interview_consent_challenge_v2(current_user, session_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CONDUCT)
    if error:
        return error
    response = _execute_mutation(
        lambda: issue_interview_consent_challenge(
            tenant,
            current_user,
            session_id,
            _json_object(),
        ),
        serialize_consent_challenge,
        "consent_challenge",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@v2_interviews_bp.route("/sessions/<int:session_id>", methods=["GET"])
@token_requerido
def get_interview_session_v2(current_user, session_id: int):
    """Return an integrity-checked checkpoint for API/channel resumption."""

    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CONDUCT)
    if error:
        return error
    try:
        session = get_interview_session(tenant, session_id)
        resume = serialize_interview_resume(session)
    except InterviewDomainError as exc:
        return _domain_error_response(exc)
    response = _json_response(
        {
            "ok": True,
            "contract_version": "assessment.interviews.api.v1",
            "resume": resume,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@v2_interviews_bp.route(
    "/sessions/<int:session_id>/consent-challenges/<int:challenge_id>/presentations",
    methods=["POST"],
)
@token_requerido
def register_interview_consent_presentation_v2(
    current_user, session_id: int, challenge_id: int
):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CONDUCT)
    if error:
        return error
    return _execute_mutation(
        lambda: register_interview_consent_presentation(
            tenant,
            current_user,
            session_id,
            challenge_id,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_consent_presentation,
        "consent_presentation",
    )


@v2_interviews_bp.route("/sessions/<int:session_id>/start", methods=["POST"])
@token_requerido
def start_interview_session_v2(current_user, session_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CONDUCT)
    if error:
        return error
    return _execute_mutation(
        lambda: start_interview_session(
            tenant,
            current_user,
            session_id,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_interview_session,
        "session",
        created_status=200,
    )


@v2_interviews_bp.route("/sessions/<int:session_id>/complete", methods=["POST"])
@token_requerido
def complete_interview_session_v2(current_user, session_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_SESSIONS_CONDUCT)
    if error:
        return error
    return _execute_mutation(
        lambda: complete_interview_session(
            tenant,
            current_user,
            session_id,
            _json_object(default_empty=True),
            _idempotency_key(),
        ),
        serialize_interview_session,
        "session",
        created_status=200,
    )


@v2_interviews_bp.route("/sessions/<int:session_id>/evidence", methods=["POST"])
@token_requerido
def create_interview_evidence_v2(current_user, session_id: int):
    tenant, error = _authorized_tenant(current_user, INTERVIEW_EVIDENCE_WRITE)
    if error:
        return error
    return _execute_mutation(
        lambda: create_interview_evidence(
            tenant,
            current_user,
            session_id,
            _json_object(),
            _idempotency_key(),
        ),
        serialize_evidence,
        "evidence",
    )
