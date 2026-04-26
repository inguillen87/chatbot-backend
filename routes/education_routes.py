from flask import Blueprint, jsonify, request
from utils.auth_helpers import token_requerido
from app import db
from models_education import School, Campus, Student, Guardian, StudentGuardianRelation
from models import TenantProfile

education_bp = Blueprint('education', __name__)

@education_bp.route('/api/v1/education/schools', methods=['GET'])
@token_requerido
def get_schools(current_user, actor_principal):
    schools = School.query.all()
    return jsonify([
        {
            "id": s.id,
            "tenant_id": s.tenant_id,
            "name": s.name
        } for s in schools
    ])

@education_bp.route('/api/v1/education/guardian/verify', methods=['POST'])
def verify_guardian():
    data = request.json or {}
    phone = data.get('phone_number')
    if not phone:
        return jsonify({"error": {"code": 400, "message": "phone_number required"}}), 400

    guardian = Guardian.query.filter_by(phone_number=phone).first()
    if not guardian:
         return jsonify({"error": {"code": 404, "message": "Guardian not found"}}), 404

    return jsonify({
        "id": guardian.id,
        "first_name": guardian.first_name,
        "last_name": guardian.last_name,
        "school_id": guardian.school_id
    })

@education_bp.route('/api/v1/education/family/context', methods=['GET'])
@token_requerido
def get_family_context(current_user, actor_principal):
    # This requires looking up by the actor's implicit identity or a passed param
    guardian_id = request.args.get('guardian_id')
    if not guardian_id:
        return jsonify({"error": {"code": 400, "message": "guardian_id required"}}), 400

    relations = StudentGuardianRelation.query.filter_by(guardian_id=guardian_id).all()

    students = []
    for r in relations:
        student = r.student
        students.append({
            "id": student.id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "grade": student.grade,
            "relationship": r.relationship_type
        })

    return jsonify({"students": students})
