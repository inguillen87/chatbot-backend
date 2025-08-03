from flask import Blueprint, jsonify, request
from services.catalog_mapping_service import catalog_mapping_service
from routes.auth import token_requerido

# Note: The user requested the URL prefix /api/pymes/:pymeId/catalog-mappings
# The :pymeId part is a dynamic parameter in Flask, written as <int:pyme_id>
catalog_mappings_bp = Blueprint('catalog_mappings', __name__, url_prefix='/api/pymes/<int:pyme_id>/catalog-mappings')

@catalog_mappings_bp.route('', methods=['GET'])
@token_requerido
def get_all_mappings(user, pyme_id):
    """Returns a list of all catalog mapping configurations for a given pymeId."""
    # To unblock the frontend immediately, this can return an empty array.
    # The service implementation will handle the actual fetching.
    mappings = catalog_mapping_service.get_all_for_pyme(pyme_id)
    return jsonify(mappings), 200

@catalog_mappings_bp.route('', methods=['POST'])
@token_requerido
def create_mapping(user, pyme_id):
    """Creates a new catalog mapping configuration."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    new_mapping = catalog_mapping_service.create(pyme_id, data)
    return jsonify(new_mapping), 201

@catalog_mappings_bp.route('/<string:mapping_id>', methods=['GET'])
@token_requerido
def get_single_mapping(user, pyme_id, mapping_id):
    """Returns the complete details for a single mapping configuration."""
    mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not mapping or mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404
    return jsonify(mapping), 200

@catalog_mappings_bp.route('/<string:mapping_id>', methods=['PUT'])
@token_requerido
def update_mapping(user, pyme_id, mapping_id):
    """Updates an existing mapping configuration."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid data"}), 400

    # First, check if the mapping exists and belongs to the pyme
    existing_mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not existing_mapping or existing_mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404

    updated_mapping = catalog_mapping_service.update(mapping_id, data)
    return jsonify(updated_mapping), 200

@catalog_mappings_bp.route('/<string:mapping_id>', methods=['DELETE'])
@token_requerido
def delete_mapping(user, pyme_id, mapping_id):
    """Deletes a mapping configuration."""
    # First, check if the mapping exists and belongs to the pyme
    existing_mapping = catalog_mapping_service.get_by_id(mapping_id)
    if not existing_mapping or existing_mapping['pymeId'] != pyme_id:
        return jsonify({"error": "Mapping not found"}), 404

    if catalog_mapping_service.delete(mapping_id):
        return '', 204
    else:
        # This case should ideally not be reached if the above check passes
        return jsonify({"error": "Mapping not found during deletion"}), 404
