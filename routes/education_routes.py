from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from extensions import db
from models import MunicipioTicket, PymeTicket, TenantProfile, TicketComentario, User
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
from utils.auth_helpers import token_requerido
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
)
from services.plan_access import (
    integration_access_payload,
    integration_plan_required_payload,
    plan_allows_integration_feature,
)

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

    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return None, (jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404)

    if not plan_allows_integration_feature(tenant, feature_id):
        return None, _education_plan_required_response(
            tenant,
            feature_id=feature_id,
            contract_version=contract_version,
        )

    return tenant, None


def _resolve_actor_tenant_id(current_user=None, actor_principal=None):
    actor = actor_principal or current_user
    tenant_id = getattr(actor, "tenant_id", None)
    if tenant_id:
        return tenant_id

    municipio_owner_id = getattr(actor, "municipio_id", None)
    if municipio_owner_id:
        tenant = TenantProfile.query.filter_by(municipio_id=municipio_owner_id).first()
        if tenant:
            return tenant.id

    actor_user_id = getattr(actor, "id", None)
    if actor_user_id:
        tenant = TenantProfile.query.filter((TenantProfile.pyme_id == actor_user_id) | (TenantProfile.municipio_id == actor_user_id)).first()
        if tenant:
            return tenant.id

    pyme_owner_id = getattr(actor, "pyme_id", None)
    if pyme_owner_id:
        tenant = TenantProfile.query.filter_by(pyme_id=pyme_owner_id).first()
        if tenant:
            return tenant.id

    return None


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
    return SchoolCaseAlias.query.filter_by(id=case_id, tenant_id=tenant_id).first()


def _ticket_for_case(alias: SchoolCaseAlias):
    if alias.ticket_type == "pyme":
        return PymeTicket.query.get(alias.ticket_id)
    return MunicipioTicket.query.get(alias.ticket_id)


def _case_payload(alias: SchoolCaseAlias, *, include_comments: bool = False) -> dict:
    ticket = _ticket_for_case(alias)
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
    if not status and assignee_id is None and not unassigned:
        return True
    ticket = _ticket_for_case(alias)
    if not ticket:
        return False
    if status and fold_text(getattr(ticket, "estado", None)) != status:
        return False
    if assignee_id is not None and getattr(ticket, "asignado_a_id", None) != assignee_id:
        return False
    if unassigned and getattr(ticket, "asignado_a_id", None):
        return False
    return True


@education_bp.route("/api/v1/education/tenant/capabilities", methods=["GET"])
@token_requerido
def get_education_capabilities(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404

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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404

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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
    payload = build_education_admin_menu(tenant)
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/whatsapp/playbook", methods=["GET"])
@token_requerido
def get_education_whatsapp_playbook(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
    payload = build_education_whatsapp_playbook(tenant)
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/operations/summary", methods=["GET"])
@token_requerido
def get_education_operations_summary(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
    payload = build_education_operations_summary(tenant)
    payload["education_enabled"] = _tenant_supports_education(tenant)
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/operations/heatmap", methods=["GET"])
@token_requerido
def get_education_operations_heatmap(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
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
    return jsonify(_education_with_access(payload, tenant))


@education_bp.route("/api/v1/education/schools", methods=["GET"])
@token_requerido
def get_schools(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    query = School.query
    if tenant_id:
        query = query.filter(School.tenant_id == tenant_id)

    schools = query.order_by(School.name.asc()).all()
    return jsonify([_school_payload(school) for school in schools])


@education_bp.route("/api/v1/education/schools/<int:school_id>", methods=["GET"])
@token_requerido
def get_school_detail(current_user, school_id: int, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404
    if tenant_id and school.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "School not available for tenant"}}), 403
    return jsonify(_school_payload(school, include_counts=True))


@education_bp.route("/api/v1/education/schools", methods=["POST"])
@token_requerido
def create_school(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    tenant = TenantProfile.query.filter_by(id=tenant_id).first()
    if not tenant:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404
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

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404
    if tenant_id and school.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "School not available for tenant"}}), 403

    campuses = Campus.query.filter_by(school_id=school.id).order_by(Campus.name.asc()).all()
    return jsonify([_campus_payload(campus) for campus in campuses])


@education_bp.route("/api/v1/education/schools/<int:school_id>/campuses", methods=["GET"])
@token_requerido
def get_school_campuses(current_user, school_id: int, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404
    if tenant_id and school.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "School not available for tenant"}}), 403

    campuses = Campus.query.filter_by(school_id=school.id).order_by(Campus.name.asc()).all()
    return jsonify([_campus_payload(campus) for campus in campuses])


@education_bp.route("/api/v1/education/campuses", methods=["POST"])
@token_requerido
def create_campus(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    campus = Campus.query.filter_by(id=campus_id).first()
    if not campus:
        return jsonify({"error": {"code": 404, "message": "Campus not found"}}), 404
    if tenant_id and campus.school and campus.school.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "Campus not available for tenant"}}), 403

    sections = CourseSection.query.filter_by(campus_id=campus.id).order_by(CourseSection.academic_year.desc()).all()
    return jsonify([_section_payload(section) for section in sections])


@education_bp.route("/api/v1/education/schools/<int:school_id>/sections", methods=["GET"])
@token_requerido
def get_school_sections(current_user, school_id: int, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404
    if tenant_id and school.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "School not available for tenant"}}), 403

    campus_ids = [campus.id for campus in school.campuses.all()]
    if not campus_ids:
        return jsonify([])
    sections = (
        CourseSection.query
        .filter(CourseSection.campus_id.in_(campus_ids))
        .order_by(CourseSection.academic_year.desc(), CourseSection.grade.asc(), CourseSection.division.asc())
        .all()
    )
    return jsonify([_section_payload(section) for section in sections])


@education_bp.route("/api/v1/education/levels", methods=["GET"])
@token_requerido
def get_academic_levels(current_user, actor_principal=None):
    school_id = request.args.get("school_id", type=int)
    if not school_id:
        return jsonify({"error": {"code": 400, "message": "school_id required"}}), 400

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    levels = AcademicLevel.query.filter_by(school_id=school.id).order_by(AcademicLevel.id.asc()).all()
    return jsonify([{"id": level.id, "school_id": level.school_id, "code": level.code, "name": level.name} for level in levels])


@education_bp.route("/api/v1/education/levels", methods=["POST"])
@token_requerido
def create_academic_level(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    school = School.query.filter_by(id=school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 404, "message": "School not found"}}), 404

    shifts = Shift.query.filter_by(school_id=school.id).order_by(Shift.id.asc()).all()
    return jsonify([{"id": shift.id, "school_id": shift.school_id, "code": shift.code, "name": shift.name} for shift in shifts])


@education_bp.route("/api/v1/education/shifts", methods=["POST"])
@token_requerido
def create_shift(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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

    campus = Campus.query.filter_by(id=campus_id).first()
    if not campus or not campus.school or campus.school.tenant_id != tenant_id:
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

    section = CourseSection(
        campus_id=campus.id,
        academic_year=academic_year,
        level_id=level.id,
        grade=grade,
        division=division,
        shift_id=shift.id,
        homeroom_staff_id=data.get("homeroom_staff_id"),
    )
    db.session.add(section)
    db.session.commit()
    return jsonify({"id": section.id, "campus_id": section.campus_id, "academic_year": section.academic_year}), 201


@education_bp.route("/api/v1/education/guardians/lookup", methods=["POST"])
@education_bp.route("/api/v1/education/guardian/lookup", methods=["POST"])
def lookup_guardian():
    data = request.json or {}
    tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    phone = (data.get("phone_number") or "").strip()
    email = (data.get("email") or "").strip()
    document = (data.get("document_number") or "").strip()

    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "tenant_id required"}}), 400
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.guardian_lookup_access.v1",
    )
    if access_response:
        return access_response
    if not any([phone, email, document]):
        return jsonify({"error": {"code": 400, "message": "phone_number, email or document_number required"}}), 400

    query = Guardian.query.filter(Guardian.tenant_id == tenant_id)
    if phone:
        query = query.filter(Guardian.phone_number == phone)
    elif email:
        query = query.filter(func.lower(Guardian.email) == email.lower())
    else:
        query = query.filter(Guardian.document_number == document)

    guardian = query.first()
    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404

    return jsonify(_guardian_payload(guardian))


@education_bp.route("/api/v1/education/guardians/verify", methods=["POST"])
@education_bp.route("/api/v1/education/guardian/verify", methods=["POST"])
def verify_guardian():
    data = request.json or {}
    tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    phone = (data.get("phone_number") or "").strip()

    if not tenant_id or not phone:
        return jsonify({"error": {"code": 400, "message": "tenant_id and phone_number required"}}), 400
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.guardian_verify_access.v1",
    )
    if access_response:
        return access_response

    guardian = Guardian.query.filter_by(tenant_id=tenant_id, phone_number=phone).first()
    status = "verified" if guardian else "failed"

    if guardian:
        guardian.verification_status = "verified"
        guardian.verification_context = {
            "source": "education.guardian.verify",
            "verified_with": "phone_number",
        }

    attempt = FamilyVerificationAttempt(
        tenant_id=tenant_id,
        guardian_id=guardian.id if guardian else None,
        verification_method="phone",
        verification_value=phone,
        status=status,
        context_json={"endpoint": request.path},
    )
    db.session.add(attempt)
    db.session.commit()

    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404

    return jsonify(_guardian_payload(guardian))


@education_bp.route("/api/v1/education/guardians/link-student", methods=["POST"])
def link_guardian_student():
    data = request.json or {}
    tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    guardian_id = _resolve_school_tenant_id(data.get("guardian_id"))
    student_id = _resolve_school_tenant_id(data.get("student_id"))
    if not tenant_id or not guardian_id or not student_id:
        return jsonify({"error": {"code": 400, "message": "tenant_id, guardian_id and student_id required"}}), 400
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.guardian_link_access.v1",
    )
    if access_response:
        return access_response

    guardian = Guardian.query.filter_by(id=guardian_id, tenant_id=tenant_id).first()
    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404
    if guardian.verification_status != "verified":
        return jsonify({"error": {"code": 403, "message": "Guardian must be verified before linking students"}}), 403

    student = Student.query.filter_by(id=student_id).first()
    if not student:
        return jsonify({"error": {"code": 404, "message": "Student not found"}}), 404
    school = School.query.filter_by(id=student.school_id, tenant_id=tenant_id).first()
    if not school:
        return jsonify({"error": {"code": 403, "message": "Student not available for tenant"}}), 403
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
    db.session.commit()

    return (
        jsonify(
            {
                "id": relation.id,
                "created": created,
                "tenant_id": tenant_id,
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
    guardian_id = request.args.get("guardian_id", type=int)
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)

    if not guardian_id and request.path.endswith("/me/family-context"):
        guardian_query = Guardian.query.filter_by(user_id=getattr(current_user, "id", None))
        if tenant_id:
            guardian_query = guardian_query.filter_by(tenant_id=tenant_id)
        guardian = guardian_query.order_by(Guardian.id.asc()).first()
        if not guardian:
            email = (getattr(current_user, "email", None) or "").strip().lower()
            phone = (getattr(current_user, "telefono", None) or "").strip()
            if email or phone:
                guardian_query = Guardian.query
                if tenant_id:
                    guardian_query = guardian_query.filter_by(tenant_id=tenant_id)
                if email:
                    guardian_query = guardian_query.filter(func.lower(Guardian.email) == email)
                elif phone:
                    guardian_query = guardian_query.filter(Guardian.phone_number == phone)
                guardian = guardian_query.order_by(Guardian.id.asc()).first()
        guardian_id = guardian.id if guardian else None

    if not guardian_id:
        return jsonify({"error": {"code": 400, "message": "guardian_id required"}}), 400

    guardian = Guardian.query.get(guardian_id)
    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404
    if tenant_id and guardian.tenant_id != tenant_id:
        return jsonify({"error": {"code": 403, "message": "Guardian not available for tenant"}}), 403

    relations = StudentGuardianRelation.query.filter_by(guardian_id=guardian_id, status="active").all()
    students = []
    for relation in relations:
        student = relation.student
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

    return jsonify(
        {
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
    data = request.json or {}
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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
        section = CourseSection.query.filter_by(id=section_id).first()
        if not section:
            return jsonify({"error": {"code": 404, "message": "Section not found"}}), 404
        if section.campus is None or section.campus.school_id != school.id:
            return jsonify({"error": {"code": 400, "message": "Section does not belong to school"}}), 400
        if campus_id is not None and section.campus_id != campus_id:
            return jsonify({"error": {"code": 400, "message": "Section does not belong to campus"}}), 400

    student = None
    if student_id is not None:
        student = Student.query.filter_by(id=student_id).first()
        if not student:
            return jsonify({"error": {"code": 404, "message": "Student not found"}}), 404
        if student.school_id != school.id:
            return jsonify({"error": {"code": 400, "message": "Student does not belong to school"}}), 400
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
            user_id=getattr(current_user, "id", None),
        )
    else:
        ticket = MunicipioTicket(
            tenant_id=tenant_id,
            municipio_id=tenant_profile.municipio_id,
            pregunta=pregunta,
            asunto=asunto,
            categoria=f"educacion:{case_type}",
            user_id=getattr(current_user, "id", None),
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400

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
    items = [_case_payload(alias) for alias in filtered_aliases[:limit]]
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
        return jsonify({"error": {"code": 404, "message": "School case not found"}}), 404
    return jsonify(_case_payload(alias, include_comments=True))


@education_bp.route("/api/v1/education/cases/<int:case_id>/reply", methods=["POST"])
@token_requerido
def reply_school_case(current_user, case_id: int, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_reply_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
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
        user_id=getattr(current_user, "id", None),
        es_admin=bool(data.get("es_admin", True)),
        origen=(data.get("origen") or "education").strip().lower(),
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_assign_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
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

    assignee = User.query.get(assignee_id)
    if not assignee:
        return jsonify({"error": {"code": 404, "message": "Assignee not found"}}), 404

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
        user_id=getattr(current_user, "id", None),
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
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    tenant, access_response = _tenant_write_access_response(
        tenant_id,
        contract_version="education.case_escalate_access.v1",
    )
    if access_response:
        return access_response
    alias = _get_case_alias_for_tenant(case_id, tenant_id)
    if not alias:
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
        user_id=getattr(current_user, "id", None),
        es_admin=True,
        origen="education",
        estado_ticket=getattr(ticket, "estado", None),
    )
    db.session.add(alias)
    db.session.add(ticket)
    db.session.add(comment)
    db.session.commit()

    return jsonify(_case_payload(alias, include_comments=True))
