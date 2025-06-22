from flask import Blueprint, jsonify, request
from models import User
from extensions import db
from routes.auth import token_requerido

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')


def _obtener_clientes(current_user: User, tag: str | None = None):
    query = User.query.filter_by(empresa_id=current_user.id)
    if tag:
        like = f"%{tag}%"
        query = query.filter(User.tags.ilike(like))
    clientes = query.order_by(User.name.asc()).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "telefono": c.telefono,
            "acepta_marketing": c.acepta_marketing,
            "latitud": c.latitud,
            "longitud": c.longitud,
            "tags": c.tags.split(',') if c.tags else [],
        }
        for c in clientes
    ]

@crm_bp.route('/clientes', methods=['GET'])
@token_requerido
def listar_clientes(current_user: User):
    """Devuelve los usuarios asociados a la empresa o municipio del token."""
    if current_user.empresa_id is not None:
        return jsonify([])
    tag = request.args.get('tag')
    resultado = _obtener_clientes(current_user, tag)
    return jsonify(resultado)


@crm_bp.route('/clientes/<int:cliente_id>/tags', methods=['PUT'])
@token_requerido
def actualizar_tags(current_user: User, cliente_id: int):
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    tags = data.get('tags', [])
    if not isinstance(tags, list):
        return jsonify({"error": "'tags' debe ser una lista"}), 400
    cliente.tags = ','.join(tags)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al actualizar"}), 500
    return jsonify({"id": cliente.id, "tags": tags})
