from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func

from extensions import db
from models import AuditEvent, MunicipioTicket, PymeTicket, TenantProfile, TicketComentario, User
from models_education import (
    AcademicLevel,
    Campus,
    CourseSection,
    FamilyVerificationAttempt,
    Guardian,
    School,
    SchoolCaseAlias,
    Shift,
    Student,
    StudentGuardianRelation,
)
from utils.auth_helpers import auth_tenant_for_user, token_requerido
from services.education_contracts import (
    build_education_admin_menu,
    build_education_profile,
    build_education_whatsapp_playbook,
    education_case_taxonomy,
    education_taxonomy_dict,
    fold_text,
    is_education_tenant,
)
from services.education_case_service import (
    build_education_operations_heatmap,
    build_education_operations_summary,
    validated_ticket_for_school_case_alias,
)
from services.tenant_ticket_scope import (
    normalize_municipio_ticket_write_scope,
)
from services.education_access_policy import (
    EDUCATION_ANALYTICS_READ,
    EDUCATION_CASES_MANAGE,
    EDUCATION_CASES_READ,
    EDUCATION_CASES_WRITE,
    EDUCATION_DIRECTORY_READ,
    EDUCATION_DIRECTORY_WRITE,
    EDUCATION_GUARDIANS_LINK,
    EDUCATION_GUARDIANS_READ,
    EDUCATION_GUARDIANS_VERIFY,
    EDUCATION_SETTINGS_READ,
    EDUCATION_SETTINGS_WRITE,
    decide_education_admin_access,
    education_staff_can_be_assigned,
    is_education_tenant_owner,
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_integration_feature,
)
from utils.auth_decorators import _is_authorized_for_tenant
from utils.roles import ROLE_CLIENTE, ROLE_LEAD, ROLE_SUPERADMIN, canonical_role

education_bp = Blueprint("education", __name__)

SCHOOL_CASE_TAXONOMY = {
    "administrativo": "Administrativo",
    "inasistencia": "Inasistencia",
    "convivencia": "Convivencia",
    "mantenimiento": "Mantenimiento",
    "documentacion": "Documentación",
    "cobranza": "Cobranza",
    "admisiones": "Admisiones",
}
SCHOOL_CASE_TAXONOMY.update(education_taxonomy_dict())

_GUARDIAN_VERIFICATION_METHODS = frozenset(
    {"in_person", "institutional_record", "external_identity_provider"}
)
_OPAQUE_EVIDENCE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,119}$")


def _utc_now():
    return datetime.now(timezone.utc)


def _tenant_supports_education(tenant: TenantProfile | None) -> bool:
    if not tenant:
        return False
    if is_education_tenant(tenant):
        return True
    vertical = (getattr(tenant, "vertical", None) or "").strip().lower()
    if vertical in {"educacion", "educación"}:
        return True
    capabilities = getattr(tenant, "capabilities_json", None)
    if isinstance(capabilities, dict):
        edu = capabilities.get("education")
        if isinstance(edu, dict):
            return bool(edu.get("enabled"))
    return False


def _education_plan_required_response(
    tenant: TenantProfile,
    *,
    feature_id: str = "education_management",
    contract_version: str = "education.integration_access.v1",
):
    return (
        jsonify(
            integration_plan_required_payload(
                tenant,
                feature_id,
                contract_version=contract_version,
                render_as="integration_locked_state",
            )
        ),
        403,
    )


def _education_with_access(payload: dict, tenant: TenantProfile) -> dict:
    payload["access"] = integration_access_payload(tenant)
    return payload


def _tenant_write_access_response(
    tenant_id: int | None,
    *,
    feature_id: str = "education_management",
    contract_version: str = "education.integration_access.v1",
):
    if not tenant_id:
        return None, (jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400)

    tenant = TenantProfile.query.filter_by(id=tenant_id, is_active=True).one_or_none()
    if not tenant:
        return None, (jsonify({"error": {"code": 403, "message": "Active tenant profile not found"}}), 403)

    if not plan_allows_integration_feature(tenant, feature_id):
        return None, _education_plan_required_response(
            tenant,
            feature_id=feature_id,
            contract_version=contract_version,
        )

    return tenant, None


def _education_access_error(
    message: str,
    status_code: int,
    reason_code: str,
    *,
    action_hint: str | None = None,
    missing_capabilities: list[str] | None = None,
):
    payload = {
        "contract_version": "education.access_error.v1",
        "status_code": status_code,
        "reason_code": reason_code,
        "retryable": False,
        "error": {"code": status_code, "message": message},
    }
    if action_hint:
        payload["action_hint"] = action_hint
    if missing_capabilities:
        payload["missing_capabilities"] = missing_capabilities
    return jsonify(payload), status_code


def _education_admin_context(current_user, actor_principal, *required: str):
    """Resolve one active tenant and authorize one education capability set."""

    actor = actor_principal or current_user
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return None, _education_access_error(
            "Tenant context required",
            400,
            "education_tenant_context_required",
            action_hint="select_tenant",
        )
    tenant = TenantProfile.query.filter_by(id=tenant_id, is_active=True).one_or_none()
    if tenant is None:
        return None, _education_access_error(
            "Active tenant profile not found",
            403,
            "education_tenant_inactive",
        )

    decision = decide_education_admin_access(actor, tenant, *required)
    if decision.allowed:
        return tenant, None
    message = (
        "Permisos insuficientes para esta operacion educativa"
        if decision.reason_code == "education_capability_required"
        else "Acceso administrativo educativo denegado"
    )
    return None, _education_access_error(
        message,
        403,
        decision.reason_code or "education_access_denied",
        action_hint="ask_tenant_admin",
        missing_capabilities=list(decision.missing_capabilities),
    )


def _guardian_operation_tenant(current_user, actor_principal, data: dict):
    actor = actor_principal or current_user
    actor_tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    declared_tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    actor_role = canonical_role(getattr(actor, "rol", None))

    # A platform admin may select an explicit tenant. All other identities are
    # bound to the tenant resolved from their authenticated principal.
    if actor_role == ROLE_SUPERADMIN and declared_tenant_id is not None:
        actor_tenant_id = declared_tenant_id
    if not actor_tenant_id:
        return None, _education_access_error(
            "Tenant context required",
            400,
            "education_tenant_context_required",
            action_hint="select_tenant",
        )
    if declared_tenant_id is not None and declared_tenant_id != actor_tenant_id:
        return None, _education_access_error(
            "Acceso denegado para el tenant solicitado",
            403,
            "education_cross_tenant_denied",
            action_hint="use_authenticated_tenant",
        )

    tenant = TenantProfile.query.filter_by(id=actor_tenant_id, is_active=True).one_or_none()
    if not tenant:
        return None, _education_access_error(
            "Active tenant profile not found",
            403,
            "education_tenant_inactive",
        )
    if not _is_authorized_for_tenant(actor, tenant_id=tenant.id, tenant_slug=tenant.slug):
        return None, _education_access_error(
            "Acceso denegado para este tenant",
            403,
            "education_cross_tenant_denied",
            action_hint="use_authenticated_tenant",
        )
    if not plan_allows_integration_feature(tenant, "education_management"):
        return None, _education_plan_required_response(
            tenant,
            feature_id="education_management",
            contract_version="education.guardian_access.v2",
        )
    return tenant, None


def _is_guardian_tenant_owner(actor, tenant: TenantProfile) -> bool:
    return is_education_tenant_owner(actor, tenant)


def _guardian_capability_response(actor, tenant: TenantProfile, *required: str):
    # Reuse the same server-authoritative role and tenant policy as every other
    # education admin surface. In particular, persisted capability-looking
    # metadata on a citizen/lead must never turn that account into staff.
    decision = decide_education_admin_access(actor, tenant, *required)
    if decision.allowed:
        return None
    reason_code = (
        "education_guardian_capability_required"
        if decision.reason_code == "education_capability_required"
        else "education_guardian_role_required"
    )
    return _education_access_error(
        "Permisos insuficientes para operar perfiles familiares",
        403,
        reason_code,
        action_hint="ask_tenant_admin",
        missing_capabilities=list(decision.missing_capabilities),
    )


def _guardian_evidence_digest(
    *, tenant_id: int, guardian_id: int, verification_method: str, evidence_ref: str
) -> str:
    secret_value = str(current_app.config.get("SECRET_KEY") or "")
    if not secret_value:
        raise RuntimeError("SECRET_KEY is required for guardian verification evidence")
    secret = secret_value.encode("utf-8")
    material = (
        f"education.guardian-proof.v1:{tenant_id}:{guardian_id}:"
        f"{verification_method}:{evidence_ref}"
    ).encode("utf-8")
    return hmac.new(secret, material, hashlib.sha256).hexdigest()


def _guardian_audit_event(
    *,
    tenant_id: int,
    actor_user_id: int | None,
    event_type: str,
    guardian_id: int | None,
    details: dict,
) -> AuditEvent:
    return AuditEvent(
        tenant_id=tenant_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        resource_type="education_guardian",
        resource_id=str(guardian_id) if guardian_id is not None else None,
        details=details,
        # IP addresses are personal data and are not required to prove this
        # institutional action. Request correlation belongs in observability.
        ip_address=None,
    )


def _commit_guardian_operation():
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Education guardian operation rolled back because its durable audit commit failed"
        )
        return _education_access_error(
            "No se pudo registrar la operacion familiar de forma segura",
            503,
            "education_guardian_audit_failed",
            action_hint="retry_later",
        )
    return None


def _resolve_actor_tenant_id(current_user=None, actor_principal=None):
    actor = actor_principal or current_user
    if actor is None:
        return None
    try:
        tenant = auth_tenant_for_user(actor)
    except Exception:
        current_app.logger.exception(
            "Unable to resolve authoritative education tenant for actor %s",
            getattr(actor, "id", None),
        )
        return None
    if tenant is None or getattr(tenant, "is_active", True) is False:
        return None
    return int(tenant.id)


def _resolve_school_tenant_id(raw_tenant_id):
    try:
        return int(raw_tenant_id)
    except (TypeError, ValueError):
        return None


def _parse_optional_int(raw_value, field_name: str):
    if raw_value in (None, ""):
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be an integer")


def _parse_bool(raw_value, default: bool = False) -> bool:
    if raw_value is None:
        return default
    if isinstance(raw_value, bool):
        return raw_value
    if isinstance(raw_value, (int, float)):
        return bool(raw_value)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"1", "true", "yes", "si", "sí", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _school_payload(school: School, *, include_counts: bool = False) -> dict:
    payload = {
        "id": school.id,
        "tenant_id": school.tenant_id,
        "name": school.name,
        "school_type": school.school_type,
        "jurisdiction": school.jurisdiction,
        "brand_name": school.brand_name,
        "status": school.status,
    }
    if include_counts:
        payload["counts"] = {
            "campuses": school.campuses.count(),
            "levels": school.academic_levels.count(),
            "shifts": school.shifts.count(),
            "students": school.students.count(),
        }
    return payload


def _campus_payload(campus: Campus) -> dict:
    return {
        "id": campus.id,
        "school_id": campus.school_id,
        "name": campus.name,
        "address": campus.address,
        "phone": campus.phone,
        "email": campus.email,
        "timezone": campus.timezone,
        "is_main": bool(campus.is_main),
    }


def _section_payload(section: CourseSection) -> dict:
    level = section.level
    shift = section.shift
    return {
        "id": section.id,
        "campus_id": section.campus_id,
        "school_id": section.campus.school_id if section.campus else None,
        "academic_year": section.academic_year,
        "grade": section.grade,
        "division": section.division,
        "level": {
            "id": level.id if level else None,
            "code": level.code if level else None,
            "name": level.name if level else None,
        },
        "shift": {
            "id": shift.id if shift else None,
            "code": shift.code if shift else None,
            "name": shift.name if shift else None,
        },
        "homeroom_staff_id": section.homeroom_staff_id,
    }


def _section_is_tenant_bound(
    section: CourseSection | None,
    school: School,
    tenant: TenantProfile,
) -> bool:
    if (
        section is None
        or section.campus is None
        or section.campus.school_id != school.id
        or section.level is None
        or section.level.school_id != school.id
        or section.shift is None
        or section.shift.school_id != school.id
    ):
        return False
    if section.homeroom_staff_id is None:
        return True
    return education_staff_can_be_assigned(
        db.session.get(User, section.homeroom_staff_id),
        tenant,
        EDUCATION_DIRECTORY_READ,
    )


def _student_is_tenant_bound(
    student: Student | None,
    school: School,
    tenant: TenantProfile,
) -> bool:
    if student is None or student.school_id != school.id:
        return False
    if student.campus_id is not None:
        if Campus.query.filter_by(id=student.campus_id, school_id=school.id).first() is None:
            return False
    if student.section_id is not None:
        section = db.session.get(CourseSection, student.section_id)
        if not _section_is_tenant_bound(section, school, tenant):
            return False
        if student.campus_id is not None and section.campus_id != student.campus_id:
            return False
    return True


def _guardian_payload(guardian: Guardian) -> dict:
    return {
        "id": guardian.id,
        "first_name": guardian.first_name,
        "last_name": guardian.last_name,
        "verification_status": guardian.verification_status,
        "tenant_id": guardian.tenant_id,
        "school_id": guardian.school_id,
        "preferred_channel": guardian.preferred_channel,
        "language": guardian.language,
    }


def _get_case_alias_for_tenant(case_id: int, tenant_id: int) -> SchoolCaseAlias | None:
    alias = SchoolCaseAlias.query.filter_by(id=case_id, tenant_id=tenant_id).first()
    if alias is None or _ticket_for_case(alias) is None:
        return None
    return alias


def _ticket_for_case(alias: SchoolCaseAlias):
    return validated_ticket_for_school_case_alias(alias)


def _case_payload(alias: SchoolCaseAlias, *, include_comments: bool = False) -> dict | None:
    ticket = _ticket_for_case(alias)
    if ticket is None:
        return None
    payload = {
        "school_case_id": alias.id,
        "school_id": alias.school_id,
        "campus_id": alias.campus_id,
        "section_id": alias.section_id,
        "student_id": alias.student_id,
        "guardian_id": alias.guardian_id,
        "case_type": alias.case_type,
        "taxonomy_label": SCHOOL_CASE_TAXONOMY.get(alias.case_type, alias.case_type),
        "sensitivity_level": alias.sensitivity_level,
        "channel": alias.channel,
        "ticket": {
            "type": alias.ticket_type,
            "id": alias.ticket_id,
            "nro_ticket": getattr(ticket, "nro_ticket", None) if ticket else None,
            "estado": getattr(ticket, "estado", None) if ticket else None,
            "asunto": getattr(ticket, "asunto", None) if ticket else None,
            "categoria": getattr(ticket, "categoria", None) if ticket else None,
            "asignado_a_id": getattr(ticket, "asignado_a_id", None) if ticket else None,
        },
    }
    if include_comments and ticket:
        payload["comments"] = [
            comment.to_dict()
            for comment in ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        ]
    return payload


def _truthy(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "si", "sí"}


def _case_matches_ticket_filters(alias: SchoolCaseAlias, *, status: str = "", assignee_id: int | None = None, unassigned: bool = False) -> bool:
    ticket = _ticket_for_case(alias)
    if not ticket:
        return False
    if not status and assignee_id is None and not unassigned:
        return True
    if status and fold_text(getattr(ticket, "estado", None)) != status:
        return False
    if assignee_id is not None and getattr(ticket, "asignado_a_id", None) != assignee_id:
        return False
    if unassigned and getattr(ticket, "asignado_a_id", None):
        return False
    return True


def _commit_case_alias_integrity_audits():
    if not db.session.info.pop("education_case_alias_audit_pending", False):
        return None
    try:
        db.session.commit()
    except Exception:
        db.session.info.pop("education_case_alias_audit_pending", None)
        db.session.rollback()
        current_app.logger.exception("Unable to persist corrupt education case alias audit")
        return _education_access_error(
            "No se pudo auditar la integridad de los casos escolares",
            503,
            "education_case_alias_audit_failed",
            action_hint="retry_later",
        )
    return None


@education_bp.route("/api/v1/education/tenant/capabilities", methods=["GET"])
@token_requerido
def get_education_capabilities(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_SETTINGS_READ
    )
    if access_response:
        return access_response

    payload = {
        "tenant_id": tenant.id,
        "tenant_slug": tenant.slug,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "education_enabled": _tenant_supports_education(tenant),
        "capabilities_json": tenant.capabilities_json or {},
        "education_profile": build_education_profile(tenant),
        "admin_menu": build_education_admin_menu(tenant),
        "whatsapp_playbook": build_education_whatsapp_playbook(tenant),
    }
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/tenant/capabilities", methods=["PUT"])
@token_requerido
def update_education_capabilities(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_SETTINGS_WRITE
    )
    if access_response:
        return access_response
    if not plan_allows_integration_feature(tenant, "education_management"):
        return _education_plan_required_response(tenant)

    payload = request.json or {}
    education_enabled = payload.get("education_enabled")
    subvertical = payload.get("subvertical")
    modules = payload.get("modules")

    if education_enabled is not None and not isinstance(education_enabled, bool):
        return jsonify({"error": {"code": 400, "message": "education_enabled must be boolean"}}), 400
    if subvertical is not None and not isinstance(subvertical, str):
        return jsonify({"error": {"code": 400, "message": "subvertical must be string"}}), 400
    if modules is not None and not isinstance(modules, list):
        return jsonify({"error": {"code": 400, "message": "modules must be list"}}), 400

    capabilities = tenant.capabilities_json if isinstance(tenant.capabilities_json, dict) else {}
    edu_capabilities = capabilities.get("education") if isinstance(capabilities.get("education"), dict) else {}

    if education_enabled is not None:
        edu_capabilities["enabled"] = education_enabled
        tenant.vertical = "educacion" if education_enabled else tenant.vertical
    if subvertical is not None:
        tenant.subvertical = subvertical.strip() or tenant.subvertical
    if modules is not None:
        edu_capabilities["modules"] = [str(item).strip() for item in modules if str(item).strip()]

    capabilities["education"] = edu_capabilities
    tenant.capabilities_json = capabilities
    db.session.add(tenant)
    db.session.commit()

    response_payload = {
        "tenant_id": tenant.id,
        "vertical": tenant.vertical,
        "subvertical": tenant.subvertical,
        "education_enabled": _tenant_supports_education(tenant),
        "capabilities_json": tenant.capabilities_json or {},
        "education_profile": build_education_profile(tenant),
        "admin_menu": build_education_admin_menu(tenant),
        "whatsapp_playbook": build_education_whatsapp_playbook(tenant),
    }
    return jsonify(_education_with_access(response_payload, tenant))


@education_bp.route("/api/v1/education/cases/taxonomy", methods=["GET"])
@token_requerido
def get_school_case_taxonomy(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_READ
    )
    if access_response:
        return access_response

    payload = {
        "tenant_id": tenant.id,
        "education_enabled": _tenant_supports_education(tenant),
        "taxonomy": [
            {"key": key, "label": label}
            for key, label in SCHOOL_CASE_TAXONOMY.items()
        ],
        "items": education_case_taxonomy(),
    }
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/admin/menu", methods=["GET"])
@token_requerido
def get_education_admin_menu(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_SETTINGS_READ
    )
    if access_response:
        return access_response
    payload = build_education_admin_menu(tenant)
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/whatsapp/playbook", methods=["GET"])
@token_requerido
def get_education_whatsapp_playbook(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_SETTINGS_READ
    )
    if access_response:
        return access_response
    payload = build_education_whatsapp_playbook(tenant)
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/operations/summary", methods=["GET"])
@token_requerido
def get_education_operations_summary(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_ANALYTICS_READ
    )
    if access_response:
        return access_response
    payload = build_education_operations_summary(tenant)
    payload["education_enabled"] = _tenant_supports_education(tenant)
    audit_response = _commit_case_alias_integrity_audits()
    if audit_response:
        return audit_response
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/operations/heatmap", methods=["GET"])
@token_requerido
def get_education_operations_heatmap(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_ANALYTICS_READ
    )
    if access_response:
        return access_response
    if not plan_allows_integration_feature(tenant, "heatmaps"):
        return _education_plan_required_response(
            tenant,
            feature_id="heatmaps",
            contract_version="education.operations_heatmap.v1",
        )
    payload = build_education_operations_heatmap(
        tenant,
        school_id=request.args.get("school_id", type=int),
        case_type=request.args.get("case_type"),
        channel=request.args.get("channel"),
        sensitivity_level=request.args.get("sensitivity_level"),
        max_points=min(request.args.get("limit", default=500, type=int) or 500, 1000),
    )
    payload["education_enabled"] = _tenant_supports_education(tenant)
    audit_response = _commit_case_alias_integrity_audits()
    if audit_response:
        return audit_response
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/schools", methods=["GET"])
@token_requerido
def get_schools(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    schools = (
        School.query.filter_by(tenant_id=tenant.id)
        .order_by(School.name.asc())
        .all()
    )
    return jsonify([_school_payload(school) for school in schools])


@education_bp.route("/api/v1/education/schools/<int:school_id>", methods=["GET"])
@token_requerido
def get_school_detail(current_user, school_id: int, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404
    return jsonify(_school_payload(school, include_counts=True))


@education_bp.route("/api/v1/education/schools", methods=["POST"])
@token_requerido
def create_school(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    if not plan_allows_integration_feature(tenant, "education_management"):
        return _education_plan_required_response(tenant)
    if not _tenant_supports_education(tenant):
        return jsonify({"error": {"code": 403, "message": "Education capability disabled for tenant"}}), 403

    data = request.json or {}
    name = str(data.get("name") or "").strip()
    school_type = str(data.get("school_type") or "public").strip().lower()
    if not name:
        return jsonify({"error": {"code": 400, "message": "name required"}}), 400
    if school_type not in {"public", "private"}:
        return jsonify({"error": {"code": 400, "message": "school_type must be public or private"}}), 400

    existing = School.query.filter(func.lower(School.name) == name.lower(), School.tenant_id == tenant_id).first()
    if existing:
        return jsonify({"error": {"code": 409, "message": "School already exists for tenant"}}), 409

    school = School(
        tenant_id=tenant_id,
        name=name,
        school_type=school_type,
        jurisdiction=(data.get("jurisdiction") or "").strip() or None,
        brand_name=(data.get("brand_name") or "").strip() or None,
        status=(data.get("status") or "active").strip().lower(),
    )
    db.session.add(school)
    db.session.commit()

    return jsonify({"id": school.id, "tenant_id": school.tenant_id, "name": school.name, "school_type": school.school_type}), 201


@education_bp.route("/api/v1/education/campuses", methods=["GET"])
@token_requerido
def get_campuses(current_user, actor_principal=None):
    school_id = request.args.get("school_id", type=int)
    if not school_id:
        return jsonify({"error": {"code": 400, "message": "school_id required"}}), 400

    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    campuses = Campus.query.filter_by(school_id=school.id).order_by(Campus.name.asc()).all()
    return jsonify([_campus_payload(campus) for campus in campuses])


@education_bp.route("/api/v1/education/schools/<int:school_id>/campuses", methods=["GET"])
@token_requerido
def get_school_campuses(current_user, school_id: int, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    campuses = Campus.query.filter_by(school_id=school.id).order_by(Campus.name.asc()).all()
    return jsonify([_campus_payload(campus) for campus in campuses])


@education_bp.route("/api/v1/education/campuses", methods=["POST"])
@token_requerido
def create_campus(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(tenant_id)
    if access_response:
        return access_response
    data = request.json or {}
    school_id = data.get("school_id")
    name = str(data.get("name") or "").strip()
    if not school_id or not name:
        return jsonify({"error": {"code": 400, "message": "school_id and name required"}}), 400

    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    existing = Campus.query.filter(Campus.school_id == school.id, func.lower(Campus.name) == name.lower()).first()
    if existing:
        return jsonify({"error": {"code": 409, "message": "Campus already exists for school"}}), 409

    campus = Campus(
        school_id=school.id,
        name=name,
        address=(data.get("address") or "").strip() or None,
        phone=(data.get("phone") or "").strip() or None,
        email=(data.get("email") or "").strip() or None,
        timezone=(data.get("timezone") or "").strip() or None,
        is_main=bool(data.get("is_main", False)),
    )
    db.session.add(campus)
    db.session.commit()
    return jsonify({"id": campus.id, "school_id": campus.school_id, "name": campus.name}), 201


@education_bp.route("/api/v1/education/sections", methods=["GET"])
@token_requerido
def get_sections(current_user, actor_principal=None):
    campus_id = request.args.get("campus_id", type=int)
    if not campus_id:
        return jsonify({"error": {"code": 400, "message": "campus_id required"}}), 400

    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    campus = (
        Campus.query.join(School, Campus.school_id == School.id)
        .filter(Campus.id == campus_id, School.tenant_id == tenant.id)
        .first()
    )
    if not campus:
        return jsonify({"error": {"code": 404, "message": "Campus not found"}}), 404

    sections = [
        section
        for section in CourseSection.query.filter_by(campus_id=campus.id)
        .order_by(CourseSection.academic_year.desc())
        .all()
        if _section_is_tenant_bound(section, campus.school, tenant)
    ]
    return jsonify([_section_payload(section) for section in sections])


@education_bp.route("/api/v1/education/schools/<int:school_id>/sections", methods=["GET"])
@token_requerido
def get_school_sections(current_user, school_id: int, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    campus_ids = [campus.id for campus in school.campuses.all()]
    if not campus_ids:
        return jsonify([])
    sections = [
        section
        for section in (
        CourseSection.query
        .filter(CourseSection.campus_id.in_(campus_ids))
        .order_by(CourseSection.academic_year.desc(), CourseSection.grade.asc(), CourseSection.division.asc())
        .all()
        )
        if _section_is_tenant_bound(section, school, tenant)
    ]
    return jsonify([_section_payload(section) for section in sections])


@education_bp.route("/api/v1/education/levels", methods=["GET"])
@token_requerido
def get_academic_levels(current_user, actor_principal=None):
    school_id = request.args.get("school_id", type=int)
    if not school_id:
        return jsonify({"error": {"code": 400, "message": "school_id required"}}), 400

    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    levels = AcademicLevel.query.filter_by(school_id=school.id).order_by(AcademicLevel.id.asc()).all()
    return jsonify([{"id": level.id, "school_id": level.school_id, "code": level.code, "name": level.name} for level in levels])


@education_bp.route("/api/v1/education/levels", methods=["POST"])
@token_requerido
def create_academic_level(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(tenant_id)
    if access_response:
        return access_response
    data = request.json or {}
    school_id = data.get("school_id")
    code = str(data.get("code") or "").strip().lower()
    name = str(data.get("name") or "").strip()

    if not school_id or not code or not name:
        return jsonify({"error": {"code": 400, "message": "school_id, code and name required"}}), 400

    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    existing = AcademicLevel.query.filter_by(school_id=school.id, code=code).first()
    if existing:
        return jsonify({"error": {"code": 409, "message": "Academic level already exists for school"}}), 409

    level = AcademicLevel(school_id=school.id, code=code, name=name)
    db.session.add(level)
    db.session.commit()
    return jsonify({"id": level.id, "school_id": level.school_id, "code": level.code, "name": level.name}), 201


@education_bp.route("/api/v1/education/shifts", methods=["GET"])
@token_requerido
def get_shifts(current_user, actor_principal=None):
    school_id = request.args.get("school_id", type=int)
    if not school_id:
        return jsonify({"error": {"code": 400, "message": "school_id required"}}), 400

    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_READ
    )
    if access_response:
        return access_response
    school = School.query.filter_by(id=school_id, tenant_id=tenant.id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    shifts = Shift.query.filter_by(school_id=school.id).order_by(Shift.id.asc()).all()
    return jsonify([{"id": shift.id, "school_id": shift.school_id, "code": shift.code, "name": shift.name} for shift in shifts])


@education_bp.route("/api/v1/education/shifts", methods=["POST"])
@token_requerido
def create_shift(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(tenant_id)
    if access_response:
        return access_response
    data = request.json or {}
    school_id = data.get("school_id")
    code = str(data.get("code") or "").strip().lower()
    name = str(data.get("name") or "").strip()

    if not school_id or not code or not name:
        return jsonify({"error": {"code": 400, "message": "school_id, code and name required"}}), 400

    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    existing = Shift.query.filter_by(school_id=school.id, code=code).first()
    if existing:
        return jsonify({"error": {"code": 409, "message": "Shift already exists for school"}}), 409

    shift = Shift(school_id=school.id, code=code, name=name)
    db.session.add(shift)
    db.session.commit()
    return jsonify({"id": shift.id, "school_id": shift.school_id, "code": shift.code, "name": shift.name}), 201


@education_bp.route("/api/v1/education/sections", methods=["POST"])
@token_requerido
def create_section(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_DIRECTORY_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(tenant_id)
    if access_response:
        return access_response
    data = request.json or {}

    campus_id = data.get("campus_id")
    level_id = data.get("level_id")
    shift_id = data.get("shift_id")
    grade = str(data.get("grade") or "").strip()
    division = str(data.get("division") or "").strip()
    academic_year = data.get("academic_year")
    if not all([campus_id, level_id, shift_id, grade, division, academic_year]):
        return jsonify({"error": {"code": 400, "message": "campus_id, level_id, shift_id, grade, division and academic_year required"}}), 400
    try:
        academic_year = int(academic_year)
    except (TypeError, ValueError):
        return jsonify({"error": {"code": 400, "message": "academic_year must be integer"}}), 400

    campus = (
        Campus.query.join(School, Campus.school_id == School.id)
        .filter(Campus.id == campus_id, School.tenant_id == tenant_id)
        .first()
    )
    if not campus:
        return jsonify({"error": {"code": 404, "message": "Campus not found"}}), 404
    level = AcademicLevel.query.filter_by(id=level_id, school_id=campus.school_id).first()
    if not level:
        return jsonify({"error": {"code": 404, "message": "Academic level not found"}}), 404
    shift = Shift.query.filter_by(id=shift_id, school_id=campus.school_id).first()
    if not shift:
        return jsonify({"error": {"code": 404, "message": "Shift not found"}}), 404

    existing = CourseSection.query.filter_by(
        campus_id=campus.id,
        academic_year=academic_year,
        level_id=level.id,
        grade=grade,
        division=division,
        shift_id=shift.id,
    ).first()
    if existing:
        return jsonify({"error": {"code": 409, "message": "Section already exists"}}), 409

    try:
        homeroom_staff_id = _parse_optional_int(
            data.get("homeroom_staff_id"), "homeroom_staff_id"
        )
    except ValueError as exc:
        return jsonify({"error": {"code": 400, "message": str(exc)}}), 400
    if homeroom_staff_id is not None:
        homeroom_staff = db.session.get(User, homeroom_staff_id)
        if not education_staff_can_be_assigned(
            homeroom_staff,
            tenant,
            EDUCATION_DIRECTORY_READ,
        ):
            return _education_access_error(
                "Homeroom staff must be privileged staff from this tenant",
                400,
                "education_homeroom_staff_invalid",
            )

    section = CourseSection(
        campus_id=campus.id,
        academic_year=academic_year,
        level_id=level.id,
        grade=grade,
        division=division,
        shift_id=shift.id,
        homeroom_staff_id=homeroom_staff_id,
    )
    db.session.add(section)
    db.session.commit()
    return jsonify({"id": section.id, "campus_id": section.campus_id, "academic_year": section.academic_year}), 201


@education_bp.route("/api/v1/education/guardians/lookup", methods=["POST"])
@education_bp.route("/api/v1/education/guardian/lookup", methods=["POST"])
@token_requerido
def lookup_guardian(current_user, actor_principal=None):
    data = request.json or {}
    actor = actor_principal or current_user
    tenant, access_response = _guardian_operation_tenant(current_user, actor_principal, data)
    if access_response:
        return access_response

    role = canonical_role(getattr(actor, "rol", None))
    query = Guardian.query.filter(Guardian.tenant_id == tenant.id)
    lookup_scope = "self"
    query_field = "authenticated_user"

    if role in {ROLE_CLIENTE, ROLE_LEAD} and not _is_guardian_tenant_owner(actor, tenant):
        # Families can only resolve a pre-provisioned guardian profile linked
        # to their authenticated user. Supplied phone/email/document values are
        # deliberately ignored so this endpoint cannot be used as an oracle.
        query = query.filter(Guardian.user_id == getattr(actor, "id", None))
    else:
        capability_response = _guardian_capability_response(
            actor, tenant, EDUCATION_GUARDIANS_READ
        )
        if capability_response:
            return capability_response
        lookup_scope = "operator"
        guardian_id = _resolve_school_tenant_id(data.get("guardian_id"))
        phone = (data.get("phone_number") or "").strip()
        email = (data.get("email") or "").strip()
        document = (data.get("document_number") or "").strip()
        supplied = [
            ("guardian_id", guardian_id),
            ("phone_number", phone),
            ("email", email),
            ("document_number", document),
        ]
        supplied = [(field, value) for field, value in supplied if value not in (None, "")]
        if len(supplied) != 1:
            return _education_access_error(
                "Provide exactly one guardian lookup field",
                400,
                "education_guardian_lookup_invalid",
                action_hint="provide_one_lookup_field",
            )
        query_field, value = supplied[0]
        if query_field == "guardian_id":
            query = query.filter(Guardian.id == value)
        elif query_field == "phone_number":
            query = query.filter(Guardian.phone_number == value)
        elif query_field == "email":
            query = query.filter(func.lower(Guardian.email) == str(value).lower())
        else:
            query = query.filter(Guardian.document_number == value)

    guardian = query.first()
    db.session.add(
        _guardian_audit_event(
            tenant_id=tenant.id,
            actor_user_id=getattr(actor, "id", None),
            event_type="education.guardian.lookup",
            guardian_id=guardian.id if guardian else None,
            details={
                "contract_version": "education.guardian_lookup.v2",
                "lookup_scope": lookup_scope,
                "query_field": query_field,
                "matched": guardian is not None,
                "query_value_persisted": False,
            },
        )
    )
    commit_response = _commit_guardian_operation()
    if commit_response:
        return commit_response

    if not guardian:
        # The public route is no longer anonymous. This generic response also
        # avoids disclosing which selector matched to authenticated families.
        return _education_access_error(
            "No hay un perfil familiar disponible para esta identidad",
            404,
            "education_guardian_profile_unavailable",
            action_hint="contact_school_staff",
        )

    payload = _guardian_payload(guardian)
    payload["contract_version"] = "education.guardian_lookup.v2"
    payload["lookup_scope"] = lookup_scope
    return jsonify(payload)


@education_bp.route("/api/v1/education/guardians/verify", methods=["POST"])
@education_bp.route("/api/v1/education/guardian/verify", methods=["POST"])
@token_requerido
def verify_guardian(current_user, actor_principal=None):
    data = request.json or {}
    actor = actor_principal or current_user
    tenant, access_response = _guardian_operation_tenant(current_user, actor_principal, data)
    if access_response:
        return access_response
    capability_response = _guardian_capability_response(
        actor, tenant, EDUCATION_GUARDIANS_VERIFY
    )
    if capability_response:
        return capability_response

    guardian_id = _resolve_school_tenant_id(data.get("guardian_id"))
    verification_method = str(data.get("verification_method") or "").strip().lower()
    evidence_ref = str(data.get("evidence_ref") or "").strip()
    if (
        not guardian_id
        or verification_method not in _GUARDIAN_VERIFICATION_METHODS
        or not _OPAQUE_EVIDENCE_REF.fullmatch(evidence_ref)
    ):
        return _education_access_error(
            "guardian_id, a supported verification_method and an opaque evidence_ref are required",
            400,
            "education_guardian_proof_required",
            action_hint="record_institutional_verification",
        )

    guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant.id).first()
    if not guardian:
        return _education_access_error(
            "Guardian not found",
            404,
            "education_guardian_not_found",
        )

    evidence_digest = _guardian_evidence_digest(
        tenant_id=tenant.id,
        guardian_id=guardian.id,
        verification_method=verification_method,
        evidence_ref=evidence_ref,
    )
    previous_status = guardian.verification_status
    guardian.verification_status = "verified"
    guardian.verification_context = {
        "contract_version": "education.guardian_verification.v2",
        "source": "institutional_attestation",
        "verification_method": verification_method,
        "evidence_ref_hmac_sha256": evidence_digest,
        "verified_by_user_id": getattr(actor, "id", None),
        "phone_ownership_verified": False,
    }

    attempt = FamilyVerificationAttempt(
        tenant_id=tenant.id,
        guardian_id=guardian.id,
        verification_method=verification_method,
        verification_value=f"hmac-sha256:{evidence_digest}",
        status="verified",
        context_json={
            "contract_version": "education.guardian_verification.v2",
            "source": "institutional_attestation",
            "verified_by_user_id": getattr(actor, "id", None),
            "phone_ownership_verified": False,
        },
    )
    db.session.add(guardian)
    db.session.add(attempt)
    db.session.add(
        _guardian_audit_event(
            tenant_id=tenant.id,
            actor_user_id=getattr(actor, "id", None),
            event_type="education.guardian.verified",
            guardian_id=guardian.id,
            details={
                "contract_version": "education.guardian_verification.v2",
                "previous_status": previous_status,
                "new_status": "verified",
                "verification_method": verification_method,
                "evidence_ref_hmac_sha256": evidence_digest,
                "raw_evidence_persisted": False,
                "phone_ownership_verified": False,
            },
        )
    )
    commit_response = _commit_guardian_operation()
    if commit_response:
        return commit_response

    payload = _guardian_payload(guardian)
    payload["contract_version"] = "education.guardian_verification.v2"
    payload["verification"] = {
        "method": verification_method,
        "proof_kind": "institutional_attestation",
        "phone_ownership_verified": False,
    }
    return jsonify(payload)


@education_bp.route("/api/v1/education/guardians/link-student", methods=["POST"])
@token_requerido
def link_guardian_student(current_user, actor_principal=None):
    data = request.json or {}
    actor = actor_principal or current_user
    tenant, access_response = _guardian_operation_tenant(current_user, actor_principal, data)
    if access_response:
        return access_response
    capability_response = _guardian_capability_response(actor, tenant, EDUCATION_GUARDIANS_LINK)
    if capability_response:
        return capability_response

    guardian_id = _resolve_school_tenant_id(data.get("guardian_id"))
    student_id = _resolve_school_tenant_id(data.get("student_id"))
    if not guardian_id or not student_id:
        return jsonify({"error": {"code": 400, "message": "guardian_id and student_id required"}}), 400

    guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant.id).first()
    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404
    if guardian.verification_status != "verified":
        return jsonify({"error": {"code": 403, "message": "Guardian must be verified before linking students"}}), 403

    student = (
        Student.query.join(School, Student.school_id == School.id)
        .filter(Student.id == student_id, School.tenant_id == tenant.id)
        .first()
    )
    if student is None or student.school is None:
        return jsonify({"error": {"code": 404, "message": "Student not found"}}), 404
    school = student.school
    if not _student_is_tenant_bound(student, school, tenant):
        return jsonify({"error": {"code": 404, "message": "Student not found"}}), 404
    if guardian.school_id and guardian.school_id != school.id:
        return jsonify({"error": {"code": 400, "message": "Guardian and student belong to different schools"}}), 400

    relation = StudentGuardianRelation.query.filter_by(
        student_id=student.id,
        guardian_id=guardian.id,
    ).first()
    created = relation is None
    if relation is None:
        relation = StudentGuardianRelation(student_id=student.id, guardian_id=guardian.id)

    relation.relationship_type = str(data.get("relationship_type") or data.get("relationship") or relation.relationship_type or "tutor")
    relation.custody_scope = data.get("custody_scope") or relation.custody_scope
    relation.can_pickup = _parse_bool(data.get("can_pickup"), bool(relation.can_pickup))
    relation.can_receive_billing = _parse_bool(data.get("can_receive_billing"), bool(relation.can_receive_billing))
    relation.can_receive_sensitive_updates = _parse_bool(
        data.get("can_receive_sensitive_updates"), bool(relation.can_receive_sensitive_updates)
    )
    relation.status = (data.get("status") or relation.status or "active").strip().lower()
    if guardian.school_id is None:
        guardian.school_id = school.id

    db.session.add(relation)
    db.session.add(guardian)
    db.session.flush()
    db.session.add(
        _guardian_audit_event(
            tenant_id=tenant.id,
            actor_user_id=getattr(actor, "id", None),
            event_type="education.guardian.student_linked",
            guardian_id=guardian.id,
            details={
                "contract_version": "education.guardian_student_link.v2",
                "student_id": student.id,
                "relation_id": relation.id,
                "created": created,
                "permissions_updated": {
                    "can_pickup": relation.can_pickup,
                    "can_receive_billing": relation.can_receive_billing,
                    "can_receive_sensitive_updates": relation.can_receive_sensitive_updates,
                },
                "pii_persisted_in_audit": False,
            },
        )
    )
    commit_response = _commit_guardian_operation()
    if commit_response:
        return commit_response

    return (
        jsonify(
            {
                "id": relation.id,
                "created": created,
                "tenant_id": tenant.id,
                "guardian": _guardian_payload(guardian),
                "student": {
                    "id": student.id,
                    "first_name": student.first_name,
                    "last_name": student.last_name,
                    "school_id": student.school_id,
                    "campus_id": student.campus_id,
                    "section_id": student.section_id,
                },
                "relationship": {
                    "type": relation.relationship_type,
                    "custody_scope": relation.custody_scope,
                    "can_pickup": relation.can_pickup,
                    "can_receive_billing": relation.can_receive_billing,
                    "can_receive_sensitive_updates": relation.can_receive_sensitive_updates,
                    "status": relation.status,
                },
            }
        ),
        201 if created else 200,
    )


@education_bp.route("/api/v1/education/me/family-context", methods=["GET"])
@education_bp.route("/api/v1/education/family/context", methods=["GET"])
@token_requerido
def get_family_context(current_user, actor_principal=None):
    actor = actor_principal or current_user
    tenant, access_response = _guardian_operation_tenant(
        current_user,
        actor_principal,
        {"tenant_id": request.args.get("tenant_id")},
    )
    if access_response:
        return access_response

    role = canonical_role(getattr(actor, "rol", None))
    is_me_route = request.path.endswith("/me/family-context")
    self_scoped = is_me_route or (
        role in {ROLE_CLIENTE, ROLE_LEAD}
        and not _is_guardian_tenant_owner(actor, tenant)
    )

    if self_scoped:
        # The authenticated user/guardian link is the only family identity
        # proof. Never infer or create that link from mutable email/phone data.
        guardian = (
            Guardian.query.filter_by(
                tenant_id=tenant.id,
                user_id=getattr(actor, "id", None),
            )
            .order_by(Guardian.id.asc())
            .first()
        )
        access_scope = "self"
    else:
        capability_response = _guardian_capability_response(
            actor, tenant, EDUCATION_GUARDIANS_READ
        )
        if capability_response:
            return capability_response
        guardian_id = request.args.get("guardian_id", type=int)
        if not guardian_id:
            return _education_access_error(
                "guardian_id required",
                400,
                "education_guardian_id_required",
                action_hint="provide_guardian_id",
            )
        # Bind id and tenant in the same query so a foreign id is never loaded
        # into the ORM session before the authorization boundary is applied.
        guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant.id).first()
        access_scope = "operator"

    if not guardian:
        return _education_access_error(
            "No hay un perfil familiar disponible para esta identidad",
            404,
            "education_guardian_profile_unavailable",
            action_hint="contact_school_staff",
        )

    relations = (
        StudentGuardianRelation.query.join(
            Student, StudentGuardianRelation.student_id == Student.id
        )
        .join(School, Student.school_id == School.id)
        .filter(
            StudentGuardianRelation.guardian_id == guardian.id,
            StudentGuardianRelation.status == "active",
            School.tenant_id == tenant.id,
        )
        .all()
    )
    students = []
    for relation in relations:
        student = relation.student
        if not _student_is_tenant_bound(student, student.school, tenant):
            continue
        students.append(
            {
                "id": student.id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "section_id": student.section_id,
                "campus_id": student.campus_id,
                "relationship": relation.relationship_type,
                "can_receive_sensitive_updates": relation.can_receive_sensitive_updates,
            }
        )

    db.session.add(
        _guardian_audit_event(
            tenant_id=tenant.id,
            actor_user_id=getattr(actor, "id", None),
            event_type="education.guardian.family_context_read",
            guardian_id=guardian.id,
            details={
                "contract_version": "education.family_context.v2",
                "access_scope": access_scope,
                "student_count": len(students),
                "pii_persisted_in_audit": False,
            },
        )
    )
    commit_response = _commit_guardian_operation()
    if commit_response:
        return commit_response

    return jsonify(
        {
            "contract_version": "education.family_context.v2",
            "access_scope": access_scope,
            "guardian": {
                "id": guardian.id,
                "first_name": guardian.first_name,
                "last_name": guardian.last_name,
                "verification_status": guardian.verification_status,
            },
            "students": students,
        }
    )


@education_bp.route("/api/v1/education/cases", methods=["POST"])
@token_requerido
def create_school_case(current_user, actor_principal=None):
    # Institutional/admin creation only. Citizen and guardian intake remains in
    # the WhatsApp/widget conversation pipeline, where identity and disclosure
    # rules differ from this case-management surface.
    data = request.json or {}
    actor = actor_principal or current_user
    tenant_profile, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant_profile.id
    tenant_profile, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_write_access.v1",
    )
    if access_response:
        return access_response

    school_id = data.get("school_id")
    case_type = (data.get("case_type") or "").strip().lower()
    asunto = (data.get("asunto") or "").strip()
    pregunta = (data.get("pregunta") or "").strip()

    if not school_id or not case_type or not asunto or not pregunta:
        return jsonify({"error": {"code": 400, "message": "school_id, case_type, asunto and pregunta are required"}}), 400
    if case_type not in SCHOOL_CASE_TAXONOMY:
        return jsonify({"error": {"code": 400, "message": "Unsupported case_type"}}), 400

    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    try:
        campus_id = _parse_optional_int(data.get("campus_id"), "campus_id")
        section_id = _parse_optional_int(data.get("section_id"), "section_id")
        student_id = _parse_optional_int(data.get("student_id"), "student_id")
        guardian_id = _parse_optional_int(data.get("guardian_id"), "guardian_id")
    except ValueError as exc:
        return jsonify({"error": {"code": 400, "message": str(exc)}}), 400

    campus = None
    if campus_id is not None:
        campus = Campus.query.filter_by(id=campus_id, school_id=school.id).first()
        if not campus:
            return jsonify({"error": {"code": 404, "message": "Campus not found for school"}}), 404

    section = None
    if section_id is not None:
        section = (
            CourseSection.query.join(Campus, CourseSection.campus_id == Campus.id)
            .join(School, Campus.school_id == School.id)
            .filter(
                CourseSection.id == section_id,
                School.id == school.id,
                School.tenant_id == tenant_id,
            )
            .first()
        )
        if not _section_is_tenant_bound(section, school, tenant_profile):
            return jsonify({"error": {"code": 404, "message": "Section not found"}}), 404
        if campus_id is not None and section.campus_id != campus_id:
            return jsonify({"error": {"code": 400, "message": "Section does not belong to campus"}}), 400

    student = None
    if student_id is not None:
        student = Student.query.filter_by(id=student_id, school_id=school.id).first()
        if not _student_is_tenant_bound(student, school, tenant_profile):
            return jsonify({"error": {"code": 404, "message": "Student not found"}}), 404
        if campus_id is not None and student.campus_id and student.campus_id != campus_id:
            return jsonify({"error": {"code": 400, "message": "Student does not belong to campus"}}), 400
        if section_id is not None and student.section_id and student.section_id != section_id:
            return jsonify({"error": {"code": 400, "message": "Student does not belong to section"}}), 400

    guardian = None
    if guardian_id is not None:
        guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant_id).first()
        if not guardian:
            return jsonify({"error": {"code": 404, "message": "Guardian not found for tenant"}}), 404
        if guardian.school_id and guardian.school_id != school.id:
            return jsonify({"error": {"code": 400, "message": "Guardian does not belong to school"}}), 400

    ticket_type = "pyme" if tenant_profile.pyme_id else "municipio"

    if ticket_type == "pyme":
        max_nro = db.session.query(func.max(PymeTicket.nro_ticket)).scalar() or 0
        ticket = PymeTicket(
            tenant_id=tenant_id,
            nro_ticket=max_nro + 1,
            pregunta=pregunta,
            asunto=asunto,
            categoria=f"educacion:{case_type}",
            user_id=getattr(actor, "id", None),
        )
    else:
        municipal_scope = normalize_municipio_ticket_write_scope(
            {
                "tenant_id": tenant_id,
                "municipio_id": tenant_profile.municipio_id,
            }
        )
        ticket = MunicipioTicket(
            tenant_id=municipal_scope["tenant_id"],
            municipio_id=municipal_scope["municipio_id"],
            pregunta=pregunta,
            asunto=asunto,
            categoria=f"educacion:{case_type}",
            user_id=getattr(actor, "id", None),
        )

    db.session.add(ticket)
    db.session.flush()

    alias = SchoolCaseAlias(
        tenant_id=tenant_id,
        school_id=school.id,
        campus_id=campus_id,
        section_id=section_id,
        student_id=student_id,
        guardian_id=guardian_id,
        case_type=case_type,
        sensitivity_level=(data.get("sensitivity_level") or "internal").strip().lower(),
        channel=(data.get("channel") or "api").strip().lower(),
        ticket_type=ticket_type,
        ticket_id=ticket.id,
    )
    db.session.add(alias)
    db.session.commit()

    return (
        jsonify(
            {
                "school_case_id": alias.id,
                "ticket_type": ticket_type,
                "ticket_id": ticket.id,
                "case_type": case_type,
                "taxonomy_label": SCHOOL_CASE_TAXONOMY[case_type],
            }
        ),
        201,
    )


@education_bp.route("/api/v1/education/cases", methods=["GET"])
@token_requerido
def list_school_cases(current_user, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_READ
    )
    if access_response:
        return access_response
    tenant_id = tenant.id

    school_id = request.args.get("school_id", type=int)
    campus_id = request.args.get("campus_id", type=int)
    section_id = request.args.get("section_id", type=int)
    student_id = request.args.get("student_id", type=int)
    guardian_id = request.args.get("guardian_id", type=int)
    case_type = fold_text(request.args.get("case_type")).replace(" ", "_")
    channel = fold_text(request.args.get("channel"))
    sensitivity_level = fold_text(request.args.get("sensitivity_level"))
    status = fold_text(request.args.get("status"))
    assignee_id = request.args.get("assignee_id", type=int)
    unassigned = _truthy(request.args.get("unassigned"))
    limit = min(request.args.get("limit", default=100, type=int) or 100, 500)
    envelope = _truthy(request.args.get("envelope"))

    query = SchoolCaseAlias.query.filter_by(tenant_id=tenant_id)
    if school_id:
        query = query.filter(SchoolCaseAlias.school_id == school_id)
    if campus_id:
        query = query.filter(SchoolCaseAlias.campus_id == campus_id)
    if section_id:
        query = query.filter(SchoolCaseAlias.section_id == section_id)
    if student_id:
        query = query.filter(SchoolCaseAlias.student_id == student_id)
    if guardian_id:
        query = query.filter(SchoolCaseAlias.guardian_id == guardian_id)
    if case_type:
        query = query.filter(SchoolCaseAlias.case_type == case_type)
    if channel:
        query = query.filter(SchoolCaseAlias.channel == channel)
    if sensitivity_level:
        query = query.filter(SchoolCaseAlias.sensitivity_level == sensitivity_level)

    fetch_limit = min(limit * 5, 1000) if (status or assignee_id is not None or unassigned) else limit
    aliases = query.order_by(SchoolCaseAlias.created_at.desc()).limit(fetch_limit).all()
    filtered_aliases = [
        alias
        for alias in aliases
        if _case_matches_ticket_filters(alias, status=status, assignee_id=assignee_id, unassigned=unassigned)
    ]
    items = [
        payload
        for alias in filtered_aliases[:limit]
        if (payload := _case_payload(alias)) is not None
    ]
    audit_response = _commit_case_alias_integrity_audits()
    if audit_response:
        return audit_response
    if not envelope:
        return jsonify(items)
    return jsonify(
        {
            "contract_version": "education.cases.list.v1",
            "items": items,
            "count": len(items),
            "limit": limit,
            "filters": {
                "school_id": school_id,
                "campus_id": campus_id,
                "section_id": section_id,
                "student_id": student_id,
                "guardian_id": guardian_id,
                "case_type": case_type or None,
                "channel": channel or None,
                "sensitivity_level": sensitivity_level or None,
                "status": status or None,
                "assignee_id": assignee_id,
                "unassigned": unassigned,
            },
        }
    )


@education_bp.route("/api/v1/education/cases/<int:case_id>", methods=["GET"])
@token_requerido
def get_school_case_detail(current_user, case_id: int, actor_principal=None):
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_READ
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
        audit_response = _commit_case_alias_integrity_audits()
        if audit_response:
            return audit_response
        return jsonify({"error": {"code": 404, "message": "School case not found"}}), 404
    return jsonify(_case_payload(alias, include_comments=True))


@education_bp.route("/api/v1/education/cases/<int:case_id>/reply", methods=["POST"])
@token_requerido
def reply_school_case(current_user, case_id: int, actor_principal=None):
    actor = actor_principal or current_user
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_WRITE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_reply_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
        audit_response = _commit_case_alias_integrity_audits()
        if audit_response:
            return audit_response
        return jsonify({"error": {"code": 404, "message": "School case not found"}}), 404
    ticket = _ticket_for_case(alias)
    if not ticket:
        return jsonify({"error": {"code": 404, "message": "Ticket not found for school case"}}), 404

    data = request.json or {}
    comentario = (data.get("comentario") or data.get("message") or data.get("body") or "").strip()
    if not comentario:
        return jsonify({"error": {"code": 400, "message": "comentario required"}}), 400

    comment = TicketComentario(
        pyme_ticket_id=ticket.id if alias.ticket_type == "pyme" else None,
        municipio_ticket_id=ticket.id if alias.ticket_type != "pyme" else None,
        comentario=comentario,
        user_id=getattr(actor, "id", None),
        es_admin=True,
        origen="education",
        estado_ticket=getattr(ticket, "estado", None),
    )
    if hasattr(ticket, "ultima_actividad"):
        ticket.ultima_actividad = _utc_now()
    db.session.add(comment)
    db.session.add(ticket)
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "school_case_id": alias.id,
            "ticket": {"type": alias.ticket_type, "id": ticket.id},
            "comment": comment.to_dict(),
        }
    ), 201


@education_bp.route("/api/v1/education/cases/<int:case_id>/assign", methods=["POST"])
@token_requerido
def assign_school_case(current_user, case_id: int, actor_principal=None):
    actor = actor_principal or current_user
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_MANAGE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_assign_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
        audit_response = _commit_case_alias_integrity_audits()
        if audit_response:
            return audit_response
        return jsonify({"error": {"code": 404, "message": "School case not found"}}), 404
    ticket = _ticket_for_case(alias)
    if not ticket:
        return jsonify({"error": {"code": 404, "message": "Ticket not found for school case"}}), 404

    data = request.json or {}
    try:
        assignee_id = _parse_optional_int(data.get("assignee_id") or data.get("asignado_a_id"), "assignee_id")
    except ValueError as exc:
        return jsonify({"error": {"code": 400, "message": str(exc)}}), 400
    if not assignee_id:
        return jsonify({"error": {"code": 400, "message": "assignee_id required"}}), 400

    assignee = db.session.get(User, assignee_id)
    if not education_staff_can_be_assigned(
        assignee,
        tenant,
        EDUCATION_CASES_WRITE,
    ):
        return _education_access_error(
            "Assignee must be privileged staff from this tenant",
            400,
            "education_assignee_invalid",
        )

    ticket.asignado_a_id = assignee.id
    ticket.asignado_en = _utc_now()
    if getattr(ticket, "estado", None) == "nuevo":
        ticket.estado = "en_proceso"
        if hasattr(ticket, "estado_cliente"):
            ticket.estado_cliente = "en_proceso"

    comment = TicketComentario(
        pyme_ticket_id=ticket.id if alias.ticket_type == "pyme" else None,
        municipio_ticket_id=ticket.id if alias.ticket_type != "pyme" else None,
        comentario=f"Caso escolar asignado a usuario #{assignee.id}",
        user_id=getattr(actor, "id", None),
        es_admin=True,
        origen="education",
        estado_ticket=getattr(ticket, "estado", None),
    )
    db.session.add(ticket)
    db.session.add(comment)
    db.session.commit()

    return jsonify(_case_payload(alias, include_comments=True))


@education_bp.route("/api/v1/education/cases/<int:case_id>/escalate", methods=["POST"])
@token_requerido
def escalate_school_case(current_user, case_id: int, actor_principal=None):
    actor = actor_principal or current_user
    tenant, access_response = _education_admin_context(
        current_user, actor_principal, EDUCATION_CASES_MANAGE
    )
    if access_response:
        return access_response
    tenant_id = tenant.id
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_escalate_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
        audit_response = _commit_case_alias_integrity_audits()
        if audit_response:
            return audit_response
        return jsonify({"error": {"code": 404, "message": "School case not found"}}), 404
    ticket = _ticket_for_case(alias)
    if not ticket:
        return jsonify({"error": {"code": 404, "message": "Ticket not found for school case"}}), 404

    data = request.json or {}
    level = (data.get("sensitivity_level") or "critical").strip().lower()
    if level not in {"sensitive", "critical"}:
        return jsonify({"error": {"code": 400, "message": "sensitivity_level must be sensitive or critical"}}), 400

    reason = (data.get("reason") or data.get("motivo") or "Escalación manual").strip()
    alias.sensitivity_level = level
    if getattr(ticket, "estado", None) == "nuevo":
        ticket.estado = "en_proceso"
        if hasattr(ticket, "estado_cliente"):
            ticket.estado_cliente = "en_proceso"

    comment = TicketComentario(
        pyme_ticket_id=ticket.id if alias.ticket_type == "pyme" else None,
        municipio_ticket_id=ticket.id if alias.ticket_type != "pyme" else None,
        comentario=f"Caso escolar escalado ({level}): {reason}",
        user_id=getattr(actor, "id", None),
        es_admin=True,
        origen="education",
        estado_ticket=getattr(ticket, "estado", None),
    )
    db.session.add(alias)
    db.session.add(ticket)
    db.session.add(comment)
    db.session.commit()

    return jsonify(_case_payload(alias, include_comments=True))
