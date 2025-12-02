from flask import Blueprint, jsonify, request
from models import Categoria, db
from routes.auth import token_requerido
from utils.permissions import require_role
# La lista de categorías disponible para asignar a los empleados y filtrar
# tickets proviene de ``services.categorias_municipio``.  Anteriormente
# se utilizaba ``TODAS_LAS_CATEGORIAS_UNICAS`` derivado de un mapa de
# palabras clave, lo que dejaba fuera categorías como ``Sugerencia`` u
# ``Otro Motivo``.  Para exponer todas las categorías relevantes al
# frontend de administración utilizamos directamente ``CATEGORIAS_RECLAMO``
# y capitalizamos cada entrada para una mejor presentación.
from services.categorias_municipio import CATEGORIAS_RECLAMO

categorias_bp = Blueprint('categorias', __name__, url_prefix='/categorias')

@categorias_bp.route('', methods=['POST'])
@token_requerido
@require_role('admin')
def crear_categoria(current_user):
    data = request.get_json()
    nombre = data.get('nombre')

    if not nombre:
        return jsonify({"error": "El nombre de la categoría es requerido."}), 400

    if not current_user.municipio_id:
        return jsonify({"error": "El usuario no está asociado a un municipio."}), 400

    nueva_categoria = Categoria(nombre=nombre, municipio_id=current_user.municipio_id)
    db.session.add(nueva_categoria)
    db.session.commit()

    return jsonify({"mensaje": "Categoría creada con éxito.", "id": nueva_categoria.id}), 201

@categorias_bp.route('', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def obtener_categorias(current_user):
    """Devuelve la lista de categorías de tickets disponibles."""
    if not current_user.municipio_id:
        return jsonify({"error": "El usuario no está asociado a un municipio."}), 400

    query = Categoria.query.filter_by(municipio_id=current_user.municipio_id)

    search_term = (request.args.get("q") or "").strip().lower()
    if search_term:
        query = query.filter(Categoria.nombre.ilike(f"%{search_term}%"))

    categorias = [c.nombre for c in query.all()]

    return jsonify({
        "categorias": categorias,
        "categories": categorias,
    })

@categorias_bp.route('/<int:categoria_id>', methods=['DELETE'])
@token_requerido
@require_role('admin')
def eliminar_categoria(current_user, categoria_id):
    if not current_user.municipio_id:
        return jsonify({"error": "El usuario no está asociado a un municipio."}), 400

    categoria = Categoria.query.filter_by(id=categoria_id, municipio_id=current_user.municipio_id).first()

    if not categoria:
        return jsonify({"error": "Categoría no encontrada."}), 404

    db.session.delete(categoria)
    db.session.commit()

    return jsonify({"mensaje": "Categoría eliminada con éxito."})
