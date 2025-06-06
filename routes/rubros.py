# En el archivo: routes/rubros.py (Versión Corregida y Final)

from flask import Blueprint, jsonify, current_app
from models import Rubro

# --- CORRECCIÓN CLAVE ---
# 1. Definimos el prefijo de la URL aquí, en el Blueprint.
# Esto le dice a Flask: "Todas las rutas en este archivo empezarán con /rubros"
rubros_bp = Blueprint('rubros', __name__, url_prefix='/rubros')

# --- CORRECCIÓN CLAVE ---
# 2. Como el prefijo ya está en el blueprint, la ruta principal es simplemente '/'
@rubros_bp.route('/', methods=['GET'])
def get_all_rubros():
    """
    Endpoint para obtener una lista de todos los rubros disponibles.
    URL final: /rubros/
    """
    try:
        rubros = Rubro.query.order_by(Rubro.nombre.asc()).all()
        lista_rubros = [{"id": rubro.id, "nombre": rubro.nombre} for rubro in rubros]
        
        # En इ तुम्हारे código, devuelves un diccionario {"rubros": [...]}. 
        # Es mejor devolver la lista directamente, es más estándar para una API.
        return jsonify(lista_rubros)

    except Exception as e:
        current_app.logger.error(f"Error al obtener la lista de rubros: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los rubros."}), 500