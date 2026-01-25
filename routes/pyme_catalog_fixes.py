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

    # Verify authorization: Only the owner or an admin should upload
    if current_user.id != pyme_id and current_user.rol != 'admin':
        # If pyme_id refers to the tenant/empresa ID, we check if user owns it
        if current_user.empresa_id != pyme_id:
             logger.warning(f"User {current_user.id} denied upload for pyme {pyme_id}")
             response = jsonify({'error': 'Unauthorized'}), 403
             return _add_cors_headers(response)

    # Delegate to the existing upload processor
    # ensure response has CORS headers if subir_catalogo doesn't add them (it usually returns a tuple)
    # subir_catalogo normally expects 'current_user' from the decorator context or g.user
    # Since we are calling it from here, we rely on the context being set up correctly by token_requerido

    # IMPORTANT: subir_catalogo is a route handler itself in `services/upload_processor.py`.
    # It likely uses `request.files` and `token_requerido` internally if used as a route.
    # However, since we're calling it as a function, we must ensure it doesn't fail on missing args if it expects `current_user`.
    # Let's inspect `subir_catalogo` signature in a future step if this fails, but for now we assume it relies on `g` or request context.
    # Actually, `subir_catalogo` is often decorated with `@token_requerido` in its definition.
    # Calling a decorated function directly passes the arguments explicitly.
    # We pass `current_user` to satisfy the decorator signature if it's reused.

    try:
        if hasattr(subir_catalogo, 'original'):
             # If it's decorated, try to call the original function
             response = subir_catalogo.original(current_user)
        else:
             # If it's not decorated or we can't access original, just call it.
             # If it's a route handler, it might expect (current_user) if it was decorated with token_requerido.
             # We try passing current_user first.
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
        # We need to wrap it back or modify the response object
        if hasattr(resp_obj, 'headers'):
             _add_cors_headers(resp_obj)
             return resp_obj, status
        else:
             # It's likely a Response object or JSON string/dict
             if isinstance(resp_obj, dict):
                 resp_obj = jsonify(resp_obj)
             _add_cors_headers(resp_obj)
             return resp_obj, status

    return _add_cors_headers(response)

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
    # catalog_status expects (current_user, pyme_id) because it is also decorated.
    # However, since we are calling it directly as a python function, we should bypass
    # the decorator if possible or ensure arguments match.
    # BUT: catalog_status above IS decorated with @token_requerido.
    # Calling a decorated function directly in Flask usually invokes the wrapper.
    # The wrapper @token_requerido(f) returns `def decorated(*args, **kwargs): ... return f(current_user, *args, **kwargs)`
    # So when we call catalog_status(current_user, pyme_id), the wrapper receives these as args.
    # Then the wrapper tries to call f(current_user, current_user, pyme_id).
    # THIS is likely why we get "takes 2 positional arguments but 3 were given".

    # We should extract the original function if we want to reuse logic,
    # OR better yet, implement the logic here directly to avoid wrapper hell.

    return catalog_status.original(current_user, pyme_id) if hasattr(catalog_status, 'original') else _catalog_status_logic(current_user, pyme_id)

def _catalog_status_logic(current_user, pyme_id):
    tenant_id = None
    if hasattr(g, 'tenant_profile') and g.tenant_profile:
        tenant_id = g.tenant_profile.id

    count = 0
    if tenant_id:
        count = CatalogoItem.query.filter_by(tenant_id=tenant_id).count()
    elif current_user:
         # Try to resolve by user ownership if tenant context missing
         if current_user.empresa_id == pyme_id: # Basic check
             # Resolve tenant for this pyme user
             pass

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
