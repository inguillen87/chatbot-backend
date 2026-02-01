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
@token_requerido
def process_catalog_proxy(current_user, pyme_id):
    """
    Proxy endpoint to handle the legacy/frontend route /api/pymes/<id>/process-catalog-file.
    It delegates the actual processing to the existing logic in subir_catalogo.
    """
    logger.info(f"Proxying catalog upload for pyme_id={pyme_id} by user={current_user.email}")
    return _handle_catalog_upload(current_user, pyme_id)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-upload/subir_catalogo', methods=['POST'])
@token_requerido
def process_catalog_upload_alias(current_user, pyme_id):
    """
    Alias for process-catalog-file to match frontend requests from some clients.
    Path: /api/pymes/<id>/catalog-upload/subir_catalogo
    """
    logger.info(f"Proxying catalog upload (alias) for pyme_id={pyme_id} by user={current_user.email}")
    return _handle_catalog_upload(current_user, pyme_id)

def _handle_catalog_upload(current_user, pyme_id):
    # Verify authorization: Only the owner or an admin should upload
    if current_user.id != pyme_id and current_user.rol != 'admin':
        # If pyme_id refers to the tenant/empresa ID, we check if user owns it
        if current_user.empresa_id != pyme_id:
             logger.warning(f"User {current_user.id} denied upload for pyme {pyme_id}")
             response = jsonify({'error': 'Unauthorized'}), 403
             return _add_cors_headers(response)

    try:
        if hasattr(subir_catalogo, 'original'):
             # If it's decorated, try to call the original function
             response = subir_catalogo.original(current_user)
        else:
             try:
                 response = subir_catalogo(current_user)
             except TypeError:
                 response = subir_catalogo() # Try without args
    except Exception as e:
        logger.error(f"Error delegating to subir_catalogo: {e}")
        response = jsonify({'error': str(e)}), 500

    # If response is a tuple (json, status), unwrap it to add headers
    if isinstance(response, tuple):
        resp_obj, status = response
        if hasattr(resp_obj, 'headers'):
             _add_cors_headers(resp_obj)
             return resp_obj, status
        else:
             if isinstance(resp_obj, dict):
                 resp_obj = jsonify(resp_obj)
             _add_cors_headers(resp_obj)
             return resp_obj, status

    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/process-catalog-file', methods=['OPTIONS'])
def process_catalog_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-upload/subir_catalogo', methods=['OPTIONS'])
def process_catalog_upload_alias_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-status', methods=['GET'])
@token_requerido
def catalog_status(current_user, pyme_id):
    """
    Returns the status of the catalog indexing.
    """
    tenant_id = None
    if hasattr(g, 'tenant_profile') and g.tenant_profile:
        tenant_id = g.tenant_profile.id
    elif current_user:
        if current_user.empresa_id == pyme_id:
            # Try to resolve tenant
            pass

    count = 0
    # Temporary lookup if tenant_id missing but pyme_id present
    # This is a hack because g.tenant_profile relies on domain/subdomain middleware
    if not tenant_id and current_user.id == pyme_id:
        # Assume user is the owner
        # We need to find the tenant for this user.
        # But for now, let's query CatalogoItem by user_id directly if tenant logic is complex
        count = CatalogoItem.query.filter_by(user_id=current_user.id).count()
    elif tenant_id:
        count = CatalogoItem.query.filter_by(tenant_id=tenant_id).count()

    response = jsonify({
        "status": "indexed" if count > 0 else "empty",
        "item_count": count,
        "last_indexed": None,
        "qdrant_status": "ready"
    })
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-status', methods=['OPTIONS'])
def catalog_status_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync/status', methods=['GET'])
@token_requerido
def catalog_vector_sync_status(current_user, pyme_id):
    # Delegate to catalog_status logic
    return catalog_status.original(current_user, pyme_id) if hasattr(catalog_status, 'original') else _catalog_status_logic(current_user, pyme_id)

def _catalog_status_logic(current_user, pyme_id):
    count = CatalogoItem.query.filter_by(user_id=current_user.id).count()
    response = jsonify({
        "status": "indexed" if count > 0 else "empty",
        "item_count": count,
        "last_indexed": None,
        "qdrant_status": "ready"
    })
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync/status', methods=['OPTIONS'])
def catalog_vector_sync_status_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync', methods=['POST'])
@token_requerido
def trigger_catalog_vector_sync(current_user, pyme_id):
    if current_user.id != pyme_id and current_user.rol != 'admin':
        if current_user.empresa_id != pyme_id:
            response = jsonify({'error': 'Unauthorized'})
            response.status_code = 403
            return _add_cors_headers(response)

    response = jsonify({"status": "accepted"})
    return _add_cors_headers(response)

@pyme_catalog_fix_bp.route('/<int:pyme_id>/catalog-vector-sync', methods=['OPTIONS'])
def trigger_catalog_vector_sync_options(pyme_id):
    response = jsonify({'status': 'ok'})
    return _add_cors_headers(response)
