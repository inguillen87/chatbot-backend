from flask import Blueprint, jsonify, request
from services.tramites import buscar_tramites

tramites_bp = Blueprint('tramites', __name__, url_prefix='/tramites')


@tramites_bp.route('', methods=['GET'])
def listar_tramites():
    """Lista los trámites disponibles ordenados por nombre."""
    q = request.args.get('q')
    return jsonify(buscar_tramites(q))


@tramites_bp.route('/<string:nombre>', methods=['GET'])
def obtener_tramite(nombre: str):
    """Devuelve la información de un trámite específico."""
    resultados = buscar_tramites(nombre)
    for item in resultados:
        if item['nombre'].lower() == nombre.lower():
            return jsonify(item)
    return jsonify({}), 404