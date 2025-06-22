from flask import Blueprint, jsonify, request, send_from_directory
from services.tramites import buscar_tramites
from services.municipios import MUNICIPIO_ID
import os

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


@tramites_bp.route('/descargar', methods=['GET'])
def descargar_tramites():
    """Devuelve el archivo JSON original de trámites para su descarga."""
    ruta = os.path.join('data', 'municipios', MUNICIPIO_ID, 'tramites.json')
    directorio = os.path.dirname(ruta)
    archivo = os.path.basename(ruta)
    return send_from_directory(directorio, archivo, as_attachment=True)

