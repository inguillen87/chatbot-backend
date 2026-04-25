from flask import Blueprint, jsonify, request
from utils.auth_helpers import token_requerido
from models_education import School, Campus, CourseSection, Guardian, StudentGuardianRelation
from models import PymeTicket
from app import db

education_bp = Blueprint('education', __name__)

@education_bp.route('/api/v1/education/schools', methods=['GET'])
@token_requerido
def list_schools(current_user, actor_principal):
    tenant_id = getattr(actor_principal, 'tenant_id', None) or getattr(actor_principal, 'municipio_id', None) or getattr(actor_principal, 'pyme_id', None)
    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'Tenant context required'}}), 400

    schools = School.query.filter_by(tenant_id=tenant_id).all()
    return jsonify([
        {
            "id": s.id,
            "name": s.name,
            "school_type": s.school_type,
            "status": s.status
        } for s in schools
    ]), 200

@education_bp.route('/api/v1/education/guardians/verify', methods=['POST'])
@token_requerido
def verify_guardian(current_user, actor_principal):
    data = request.json or {}
    document_number = data.get('document_number')
    tenant_id = getattr(actor_principal, 'tenant_id', None) or getattr(actor_principal, 'municipio_id', None) or getattr(actor_principal, 'pyme_id', None)

    if not document_number or not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'Missing document_number or tenant context'}}), 400

    guardian = Guardian.query.filter_by(tenant_id=tenant_id, document_number=document_number).first()
    if guardian:
        guardian.verification_status = "verified"
        db.session.commit()
        return jsonify({
            "status": "verified",
            "guardian_id": guardian.id,
            "name": f"{guardian.first_name} {guardian.last_name}"
        }), 200

    return jsonify({'error': {'code': 404, 'message': 'Guardian not found for verification'}}), 404

@education_bp.route('/api/v1/education/me/family-context', methods=['GET'])
@token_requerido
def get_family_context(current_user, actor_principal):
    guardian_id = request.args.get('guardian_id', type=int)
    if not guardian_id:
        return jsonify({'error': {'code': 400, 'message': 'Missing guardian_id'}}), 400

    relations = StudentGuardianRelation.query.filter_by(guardian_id=guardian_id, status='active').all()
    students = []
    for rel in relations:
        student = rel.student
        students.append({
            "id": student.id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "section_id": student.section_id,
            "relationship": rel.relationship_type
        })

    return jsonify({"students": students}), 200

@education_bp.route('/api/v1/education/cases', methods=['GET'])
@token_requerido
def list_cases(current_user, actor_principal):
    # This aliases to existing tickets. We use PymeTicket as the universal case engine for this tenant context
    tenant_id = getattr(actor_principal, 'tenant_id', None) or getattr(actor_principal, 'municipio_id', None) or getattr(actor_principal, 'pyme_id', None)
    if not tenant_id:
        return jsonify({'error': {'code': 400, 'message': 'Tenant context required'}}), 400

    cases = PymeTicket.query.filter_by(tenant_id=tenant_id).all()
    return jsonify([
        {
            "id": c.id,
            "subject": c.asunto,
            "category": c.categoria,
            "status": c.estado
        } for c in cases
    ]), 200
