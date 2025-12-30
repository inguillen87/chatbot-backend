from flask import Blueprint, request, jsonify, g
from services.upload_processor import subir_catalogo
from routes.auth import token_requerido
from models import User, CatalogoItem
import logging

logger = logging.getLogger(__name__)

pyme_catalog_fix_bp = Blueprint('pyme_catalog_fix_bp', __name__, url_prefix='/api/pymes')

def _add_cors_headers(response):
    origin = request.headers.get('Origin', '*')
    response.headers.add('Access-Control-Allow-Origin', origin)
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Tenant,X-Requested-With,X-Anon-Id')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS,PUT,DELETE')
    response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response

@pyme_catalog_fix_bp.route('/<int:pyme_id>/process-catalog-file', methods=['POST'])
def process_catalog_proxy(pyme_id):
    """
    Proxy endpoint to handle the legacy/frontend route /api/pymes/<id>/process-catalog-file.
    It delegates the actual processing to the existing logic in subir_catalogo.
    """
    logger.info(f"Proxying catalog upload for pyme_id={pyme_id}")

    # Delegate to the existing upload processor
    # ensure response has CORS headers if subir_catalogo doesn't add them (it usually returns a tuple)
    response = subir_catalogo()

    # If response is a tuple (json, status), unwrap it
    if isinstance(response, tuple):
        resp_obj, status = response
        if not hasattr(resp_obj, 'headers'):
             # If it's a dict or string, jsonify it if needed, but subir_catalogo returns jsonify usually
             pass
        return response # subir_catalogo typically handles response format

    return response

@pyme_catalog_fix_bp.route('/<int:pyme_id>/process-catalog-file', methods=['OPTIONS'])
def process_catalog_options(pyme_id):
    """
    Handle CORS preflight for this specific route.
    """
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-status', methods=['GET'])
@token_requerido
def catalog_status(current_user, pyme_id):
    """
    Returns the status of the catalog indexing.
    """
    # Assuming the current tenant context is set correctly by middleware
    # or we derive it from current_user

    tenant_id = None
    if hasattr(g, 'tenant_profile') and g.tenant_profile:
        tenant_id = g.tenant_profile.id
    elif current_user:
        # Fallback: try to find tenant for this user
        # This might be tricky if user has multiple, but usually pyme owner has one
        # For now, rely on g.tenant_profile or return 0
        pass

    count = 0
    if tenant_id:
        count = CatalogoItem.query.filter_by(tenant_id=tenant_id).count()

    response = jsonify({
        "status": "indexed" if count > 0 else "empty",
        "item_count": count,
        "last_indexed": None, # Implement actual timestamp if available
        "qdrant_status": "ready" # Mock status
    })
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-status', methods=['OPTIONS'])
def catalog_status_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync/status', methods=['GET'])
@token_requerido
def catalog_vector_sync_status(current_user, pyme_id):
    """
    Alias for catalog-status to match frontend requests.
    """
    return catalog_status(current_user, pyme_id)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync/status', methods=['OPTIONS'])
def catalog_vector_sync_status_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)
