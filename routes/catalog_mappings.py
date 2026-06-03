from flask import Blueprint, jsonify, request
from models import TenantProfile
from services.catalog_mapping_service import catalog_mapping_service
from routes.auth import token_requerido
from services.plan_access import integration_access_payload, plan_allows_full_integrations

def _options_ok():
    return "", 204


def _tenant_for_pyme(pyme_id: int):
    return TenantProfile.query.filter_by(pyme_id=pyme_id).first()


def _catalog_plan_required_response(pyme_id: int):
    tenant = _tenant_for_pyme(pyme_id)
    access = integration_access_payload(tenant)
    feature = (access.get("features") or {}).get("catalog_management") or {}
    response = jsonify(
        {
            "error": "plan_required",
            "message": access.get("message") or "Tu plan actual no habilita gestion productiva de catalogo.",
            "feature": feature,
            "access": access,
            "frontend_contract": {
                "render_as": "integration_locked",
                "primary_action": "upgrade_to_full",
                "feature_id": "catalog_management",
            },
        }
    )
    response.status_code = 403
    return response


def _catalog_writes_allowed(pyme_id: int) -> bool:
    return plan_allows_full_integrations(_tenant_for_pyme(pyme_id))


# Note: The user requested the URL prefix /api/pymes/:pymeId/catalog-mappings
# The :pymeId part is a dynamic parameter in Flask, written as <int:pyme_id>
catalog_mappings_bp = Blueprint('catalog_mappings', __name__, url_prefix='/api/pymes/<int:pyme_id>/catalog-mappings')
catalog_mappings_public_bp = Blueprint(
    'catalog_mappings_public', __name__, url_prefix='/pymes/<int:pyme_id>/catalog-mappings'
)


@catalog_mappings_bp.route('', methods=['OPTIONS'])
def catalog_mappings_options(pyme_id):
    """Return an empty 204 for CORS preflight on the API prefix."""

    return _options_ok()

def _get_all_mappings(pyme_id):
    """Returns a list of all catalog mapping configurations for a given pymeId."""
    if request.method == 'OPTIONS':
        return '', 204

    mappings = catalog_mapping_service.get_all_for_pyme(pyme_id)
    return jsonify(mappings), 200


def _create_mapping(pyme_id):
    """Creates a new catalog mapping configuration."""
    if request.method == 'OPTIONS':
        return '', 204

    if not _catalog_writes_allowed(pyme_id):
        return _catalog_plan_required_response(pyme_id)

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    new_mapping = catalog_mapping_service.create(pyme_id, data)
    return jsonify(new_mapping), 201


def _get_single_mapping(pyme_id, mapping_id):
    """Returns the complete details for a single mapping configuration."""
    if request.method == 'OPTIONS':
        return '', 204

    mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not mapping or mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404
    return jsonify(mapping), 200


def _update_mapping(pyme_id, mapping_id):
    """Updates an existing mapping configuration."""
    if request.method == 'OPTIONS':
        return '', 204

    if not _catalog_writes_allowed(pyme_id):
        return _catalog_plan_required_response(pyme_id)

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    # First, check if the mapping exists and belongs to the pyme
    existing_mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not existing_mapping or existing_mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404

    updated_mapping = catalog_mapping_service.update(mapping_id, data)
    return jsonify(updated_mapping), 200


def _delete_mapping(pyme_id, mapping_id):
    """Deletes a mapping configuration."""
    if request.method == 'OPTIONS':
        return '', 204

    if not _catalog_writes_allowed(pyme_id):
        return _catalog_plan_required_response(pyme_id)

    # First, check if the mapping exists and belongs to the pyme
    existing_mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not existing_mapping or existing_mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404

    if catalog_mapping_service.delete(mapping_id):
        return '', 204
    else:
        # This case should ideally not be reached if the above check passes
        return jsonify({"error": "Mapping not found during deletion"}), 404

@catalog_mappings_bp.route('', methods=['GET'])
@token_requerido
def get_all_mappings(user, pyme_id):
    return _get_all_mappings(pyme_id)


@catalog_mappings_bp.route('', methods=['POST'])
@token_requerido
def create_mapping(user, pyme_id):
    return _create_mapping(pyme_id)


@catalog_mappings_bp.route('/<string:mapping_id>', methods=['GET'])
@token_requerido
def get_single_mapping(user, pyme_id, mapping_id):
    return _get_single_mapping(pyme_id, mapping_id)


@catalog_mappings_bp.route('/<string:mapping_id>', methods=['PUT'])
@token_requerido
def update_mapping(user, pyme_id, mapping_id):
    return _update_mapping(pyme_id, mapping_id)


@catalog_mappings_bp.route('/<string:mapping_id>', methods=['DELETE'])
@token_requerido
def delete_mapping(user, pyme_id, mapping_id):
    return _delete_mapping(pyme_id, mapping_id)


@catalog_mappings_public_bp.route('', methods=['OPTIONS'])
def catalog_mappings_public_options(pyme_id):
    """Return an empty 204 for CORS preflight on the public prefix."""

    return _options_ok()


@catalog_mappings_public_bp.route('', methods=['GET'])
@token_requerido
def get_all_mappings_public(user, pyme_id):
    """Expose GET mappings under /pymes to match the widget paths."""

    return _get_all_mappings(pyme_id)


@catalog_mappings_public_bp.route('', methods=['POST'])
@token_requerido
def create_mapping_public(user, pyme_id):
    """Expose POST mappings under /pymes to match the widget paths."""

    return _create_mapping(pyme_id)


@catalog_mappings_public_bp.route('/<string:mapping_id>', methods=['GET'])
@token_requerido
def get_single_mapping_public(user, pyme_id, mapping_id):
    """Expose single mapping fetch under /pymes to match the widget paths."""

    return _get_single_mapping(pyme_id, mapping_id)


@catalog_mappings_public_bp.route('/<string:mapping_id>', methods=['PUT'])
@token_requerido
def update_mapping_public(user, pyme_id, mapping_id):
    """Expose update under /pymes to match the widget paths."""

    return _update_mapping(pyme_id, mapping_id)


@catalog_mappings_public_bp.route('/<string:mapping_id>', methods=['DELETE'])
@token_requerido
def delete_mapping_public(user, pyme_id, mapping_id):
    """Expose delete under /pymes to match the widget paths."""

    return _delete_mapping(pyme_id, mapping_id)
