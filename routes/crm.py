from flask import Blueprint, jsonify
from models import User
from routes.auth import token_requerido

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')

@crm_bp.route('/clientes', methods=['GET'])
@token_requerido
def listar_clientes(current_user: User):
    """Devuelve los usuarios asociados a la empresa o municipio del token."""
    if current_user.empresa_id is not None:
        return jsonify([])
    clientes = (
        User.query.filter_by(empresa_id=current_user.id)
        .order_by(User.name.asc())
        .all()
    )
    resultado = [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "telefono": c.telefono,
            "acepta_marketing": c.acepta_marketing,
            "latitud": c.latitud,
            "longitud": c.longitud,
        }
        for c in clientes
    ]
    return jsonify(resultado)
