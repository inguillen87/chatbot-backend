from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import func

from app import db
from models import MunicipioTicket, PymeTicket, TenantProfile
from models_education import (
    AcademicLevel,
    Campus,
    CourseSection,
    FamilyVerificationAttempt,
    Guardian,
    School,
    SchoolCaseAlias,
    StudentGuardianRelation,
)
from utils.auth_helpers import token_requerido

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


@education_bp.route("/api/v1/education/schools", methods=["GET"])
@token_requerido
def get_schools(current_user, actor_principal=None):
    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
    query = School.query
    if tenant_id:
        query = query.filter(School.tenant_id == tenant_id)

    schools = query.order_by(School.name.asc()).all()
    return jsonify(
        [
            {
                "id": school.id,
                "tenant_id": school.tenant_id,
                "name": school.name,
                "school_type": school.school_type,
                "jurisdiction": school.jurisdiction,
                "status": school.status,
            }
            for school in schools
        ]
    )


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
    return jsonify(
        [
            {
                "id": campus.id,
                "school_id": campus.school_id,
                "name": campus.name,
                "address": campus.address,
                "is_main": bool(campus.is_main),
            }
            for campus in campuses
        ]
    )


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
    response = []
    for section in sections:
        level = AcademicLevel.query.get(section.level_id)
        response.append(
            {
                "id": section.id,
                "campus_id": section.campus_id,
                "academic_year": section.academic_year,
                "grade": section.grade,
                "division": section.division,
                "level": {
                    "id": level.id if level else None,
                    "code": level.code if level else None,
                    "name": level.name if level else None,
                },
            }
        )
    return jsonify(response)


@education_bp.route("/api/v1/education/guardian/lookup", methods=["POST"])
def lookup_guardian():
    data = request.json or {}
    tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    phone = (data.get("phone_number") or "").strip()
    email = (data.get("email") or "").strip()
    document = (data.get("document_number") or "").strip()

    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "tenant_id required"}}), 400
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

    return jsonify(
        {
            "id": guardian.id,
            "first_name": guardian.first_name,
            "last_name": guardian.last_name,
            "verification_status": guardian.verification_status,
            "tenant_id": guardian.tenant_id,
        }
    )


@education_bp.route("/api/v1/education/guardian/verify", methods=["POST"])
def verify_guardian():
    data = request.json or {}
    tenant_id = _resolve_school_tenant_id(data.get("tenant_id"))
    phone = (data.get("phone_number") or "").strip()

    if not tenant_id or not phone:
        return jsonify({"error": {"code": 400, "message": "tenant_id and phone_number required"}}), 400

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
        context_json={"endpoint": "/api/v1/education/guardian/verify"},
    )
    db.session.add(attempt)
    db.session.commit()

    if not guardian:
        return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404

    return jsonify(
        {
            "id": guardian.id,
            "first_name": guardian.first_name,
            "last_name": guardian.last_name,
            "verification_status": guardian.verification_status,
            "tenant_id": guardian.tenant_id,
        }
    )


@education_bp.route("/api/v1/education/family/context", methods=["GET"])
@token_requerido
def get_family_context(current_user, actor_principal=None):
    guardian_id = request.args.get("guardian_id", type=int)
    if not guardian_id:
        return jsonify({"error": {"code": 400, "message": "guardian_id required"}}), 400

    tenant_id = _resolve_actor_tenant_id(current_user, actor_principal)
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
    if not tenant_id:
        return jsonify({"error": {"code": 400, "message": "Tenant context required"}}), 400

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

    tenant_profile = TenantProfile.query.get(tenant_id)
    if not tenant_profile:
        return jsonify({"error": {"code": 404, "message": "Tenant profile not found"}}), 404

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
        campus_id=data.get("campus_id"),
        section_id=data.get("section_id"),
        student_id=data.get("student_id"),
        guardian_id=data.get("guardian_id"),
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
    query = SchoolCaseAlias.query.filter_by(tenant_id=tenant_id)
    if school_id:
        query = query.filter(SchoolCaseAlias.school_id == school_id)

    aliases = query.order_by(SchoolCaseAlias.created_at.desc()).limit(100).all()
    return jsonify(
        [
            {
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
                },
            }
            for alias in aliases
        ]
    )
