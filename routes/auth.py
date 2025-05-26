from flask import Blueprint, request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, Rubro
from extensions import db
from functools import wraps
import uuid
import logging
import traceback

auth_bp = Blueprint('auth', __name__)

# Decorador de autenticación robusto
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not token:
            return jsonify({"error": "Token faltante"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            return jsonify({"error": "Token inválido"}), 403

        return f(user, *args, **kwargs)
    return decorated

# Endpoint para obtener datos del usuario actual
import traceback  # asegurate de tener esto arriba

@auth_bp.route('/me', methods=['GET'])
def get_current_user():
    from flask import current_app
    import traceback

    token = request.headers.get("Authorization", "").replace("Bearer ", "").strip()

    if not token:
        return jsonify({"error": "Token faltante"}), 401

    user = User.query.filter_by(token=token).first()
    if not user:
        return jsonify({"error": "Token inválido"}), 401

    try:
        rubro_nombre = "General"
        rubro = Rubro.query.get(user.rubro_id) if user.rubro_id else None
        if rubro:
            rubro_nombre = rubro.nombre

        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": user.name or "",
            "email": user.email or "",
            "plan": user.plan or "gratis",
            "preguntas_usadas": user.preguntas_usadas or 0,
            "limite_preguntas": user.limite_preguntas or 0,
            "nombre_empresa": user.nombre_empresa or "",
            "direccion": user.direccion or "",
            "telefono": user.telefono or "",
            "link_web": user.link_web or "",
            "horario": user.horario or "",
            "ubicacion": user.ubicacion or "",
            "logo_url": user.logo_url or "",
            "rubro": rubro_nombre
        })

    except Exception as e:
        error_trace = traceback.format_exc()
        print("❌ ERROR EN /me")
        print(error_trace)
        current_app.logger.error("❌ Error crítico en /me:\n" + error_trace)
        return jsonify({"error": "Error interno al obtener perfil"}), 500


# Actualizar perfil
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_profile(user):
    try:
        data = request.get_json()

        user.nombre_empresa = (data.get("nombre_empresa") or user.nombre_empresa or "").strip()
        user.telefono = (data.get("telefono") or user.telefono or "").strip()
        user.direccion = (data.get("direccion") or user.direccion or "").strip()
        user.ubicacion = (data.get("ubicacion") or user.ubicacion or "").strip()
        user.horario = (data.get("horario") or user.horario or "").strip()
        user.link_web = (data.get("link_web") or user.link_web or "").strip()
        user.logo_url = (data.get("logo_url") or user.logo_url or "").strip()

        db.session.commit()
        logging.info(f"✅ Perfil actualizado: {user.email}")
        return jsonify({"mensaje": "Perfil actualizado correctamente"})
    except Exception as e:
        logging.error("❌ Error al actualizar perfil: %s", traceback.format_exc())
        return jsonify({"error": f"Error interno al guardar los datos"}), 500

# Crear base directo si no existen migraciones
@auth_bp.cli.command("crear_base")
def crear_base():
    db.create_all()
    print("Base creada directamente desde los modelos.")
