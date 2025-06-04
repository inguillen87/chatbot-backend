# En routes/auth.py
from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import func # <--- AÑADIDO PARA BÚSQUEDA CASE-INSENSITIVE DE RUBRO
from models import User, Rubro, CatalogoItem # CatalogoItem se usa en /debug/users
from extensions import db
from functools import wraps
import uuid
import logging 
import traceback
from datetime import datetime
import json # Para validar el string de horario_json

auth_bp = Blueprint('auth', __name__)

# Decorador para verificar token en rutas protegidas
def token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token_full = request.headers.get("Authorization")
        token = None
        if token_full and token_full.startswith("Bearer "):
            token = token_full.split(" ")[1]
        
        if not token:
            logging.warning("Intento de acceso sin token.")
            return jsonify({"error": "Token faltante"}), 401

        user = User.query.filter_by(token=token).first()
        if not user:
            # Loguear solo parte del token para seguridad
            logging.warning(f"Intento de acceso con token inválido: {token[:15]}...") 
            return jsonify({"error": "Token inválido o sesión expirada"}), 403

        return f(user, *args, **kwargs)
    return decorated

# Obtener información del usuario actual
@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user): # user es inyectado por @token_requerido
    try:
        rubro_nombre = "General"
        if user.rubro_id:
            rubro = db.session.get(Rubro, user.rubro_id)
            if rubro:
                rubro_nombre = rubro.nombre

        user_data_response = {
            "token": user.token,
            "id": user.id,
            "name": getattr(user, "name", "") or "",
            "email": getattr(user, "email", "") or "",
            "plan": getattr(user, "plan", "gratis") or "gratis",
            "preguntas_usadas": getattr(user, "preguntas_usadas", 0) or 0,
            "limite_preguntas": getattr(user, "limite_preguntas", 50) or 50,
            "nombre_empresa": getattr(user, "nombre_empresa", "") or "",
            "telefono": getattr(user, "telefono", "") or "",
            "direccion": getattr(user, "direccion", "") or "",
            "ciudad": getattr(user, "ciudad", "") or "",
            "provincia": getattr(user, "provincia", "") or "",
            "pais": getattr(user, "pais", "") or "",
            "latitud": getattr(user, "latitud", None),
            "longitud": getattr(user, "longitud", None),
            "link_web": getattr(user, "link_web", "") or "",
            "horario_json": user.horario_json, # Llama a la @property del modelo User
            "logo_url": getattr(user, "logo_url", "") or "",
            "rubro": rubro_nombre
        }
        return jsonify(user_data_response)
    except Exception as e:
        logger = current_app.logger if hasattr(current_app, 'logger') else logging
        logger.error(f"❌ Error crítico en GET /me para user_id {user.id}:\n{traceback.format_exc()}")
        return jsonify({"error": "Error interno al obtener el perfil del usuario."}), 500


# Endpoint debug: ver todos los usuarios y su catálogo
@auth_bp.route('/debug/users', methods=['GET'])
# Considera añadir un decorador de admin_requerido para este endpoint si lo expones
def debug_usuarios():
    try:
        usuarios = User.query.all()
        resultado = []
        for u in usuarios:
            rubro = db.session.get(Rubro, u.rubro_id) if u.rubro_id else None
            items = CatalogoItem.query.filter_by(user_id=u.id).all()
            catalogo_data = [{
                "nombre": item.nombre, "descripcion": item.descripcion, "precio": item.precio,
                "cantidad": item.cantidad, "categoria": item.categoria or "", "unidad": item.unidad or ""
            } for item in items]
            
            resultado.append({
                "id": u.id, "token": u.token, "name": u.name, "email": u.email,
                "empresa": u.nombre_empresa, "rubro": rubro.nombre if rubro else "General",
                "telefono": u.telefono, "direccion": u.direccion, "link_web": u.link_web,
                "horario_simple": u.horario, # El string simple de horario (directo de la BD)
                "horario_estructurado": u.horario_json, # El JSON de horarios (parseado por la @property)
                "catalogo_items_count": len(catalogo_data)
                # "catalogo": catalogo_data # Descomentar si quieres ver todo el catálogo
            })
        return jsonify(resultado)
    except Exception as e:
        logger = current_app.logger if hasattr(current_app, 'logger') else logging
        logger.error(f"❌ Error crítico en /debug/users:\n{traceback.format_exc()}")
        return jsonify({"error": "Error interno en la ruta de debug."}), 500


# Registro de usuario
@auth_bp.route('/register', methods=['POST'])
def register():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Solicitud JSON inválida o vacía."}), 400

        name = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        password = data.get("password", "").strip()
        nombre_empresa = data.get("nombre_empresa", "").strip()
        rubro_nombre = data.get("rubro", "").strip()
        acepto_terminos = bool(data.get("acepto_terminos", False))
        fecha_aceptacion_terminos = data.get("fecha_aceptacion_terminos")

        if not all([name, email, password, nombre_empresa, rubro_nombre, acepto_terminos]):
            return jsonify({"error": "Todos los campos son obligatorios y es necesario aceptar los términos."}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Ya existe un usuario con ese correo electrónico."}), 409

        rubro = Rubro.query.filter(func.lower(Rubro.nombre) == func.lower(rubro_nombre)).first()
        if not rubro:
            return jsonify({"error": f"El rubro '{rubro_nombre}' no es válido o no se encontró."}), 400

        # Si no viene fecha explícita, se pone ahora
        if not fecha_aceptacion_terminos:
            fecha_aceptacion_terminos = datetime.utcnow()
        else:
            fecha_aceptacion_terminos = datetime.fromisoformat(fecha_aceptacion_terminos)

        user = User(
            name=name,
            email=email,
            token=str(uuid.uuid4()),
            nombre_empresa=nombre_empresa,
            rubro_id=rubro.id,
            plan="gratis",
            preguntas_usadas=0,
            limite_preguntas=50,
            acepto_terminos=acepto_terminos,
            fecha_aceptacion_terminos=fecha_aceptacion_terminos
        )
        user.set_password(password)

        db.session.add(user)
        db.session.commit()
        logging.info(f"✅ Usuario registrado: {email} con ID {user.id}")

        return jsonify({
            "mensaje": "Usuario registrado exitosamente.",
            "token": user.token,
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan,
            "nombre_empresa": user.nombre_empresa,
            "rubro_id": user.rubro_id,
            "rubro": rubro.nombre,
            "preguntas_usadas": user.preguntas_usadas,
            "limite_preguntas": user.limite_preguntas
        }), 201
    except Exception as e:
        logging.error(f"❌ Error en /register:\n{traceback.format_exc()}")
        return jsonify({"error": "Error interno al registrar el usuario."}), 500
    
# Actualizar perfil del usuario
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_profile(current_user):
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibieron datos válidos en formato JSON."}), 400

        logging.info(f"📥 DATOS RECIBIDOS en PUT /perfil para user {current_user.email}: {data}")

        campos_info = {
            "nombre_empresa": str, "telefono": str, "direccion": str,
            "ciudad": str, "provincia": str, "pais": str,
            "latitud": float, "longitud": float,
            "link_web": str, 
            "horario_json": str, # Clave que se espera en el request para el horario
            "logo_url": str
        }
        
        cambios_realizados_log = {}

        for campo_request, tipo_esperado in campos_info.items():
            if campo_request in data:
                valor_recibido = data[campo_request]
                
                if isinstance(valor_recibido, str):
                    valor_recibido = valor_recibido.strip()
                    if valor_recibido == "":
                        valor_recibido = None 
                
                # Procesamiento especial para horario_json
                if campo_request == "horario_json":
                    campo_modelo_db = "horario" # El campo en la BD es 'horario'
                    if valor_recibido is not None:
                        if not isinstance(valor_recibido, str):
                            return jsonify({"error": f"El campo '{campo_request}' debe ser un string JSON."}), 400
                        try:
                            # Validar la estructura del JSON del horario
                            parsed_horarios = json.loads(valor_recibido)
                            if not isinstance(parsed_horarios, list): # Ejemplo de validación: debe ser una lista
                                raise ValueError("El JSON de horario debe ser una lista de objetos.")
                            for item_horario in parsed_horarios: # Ejemplo: cada item es un dict con claves
                                if not isinstance(item_horario, dict) or \
                                   not all(k in item_horario for k in ["dia", "abre", "cierra", "cerrado"]): # Ajusta según tu estructura
                                    raise ValueError("Cada ítem de horario debe ser un dict con 'dia', 'abre', 'cierra', 'cerrado'.")
                            # Si es válido, 'valor_recibido' (el string JSON) se asignará a current_user.horario
                        except (json.JSONDecodeError, ValueError) as e_json:
                            logging.warning(f"Formato de {campo_request} inválido para user {current_user.email}: {valor_recibido}. Error: {e_json}")
                            return jsonify({"error": f"El formato de '{campo_request}' es inválido. Detalles: {str(e_json)}"}), 400
                    setattr(current_user, campo_modelo_db, valor_recibido) # Guardar en user.horario
                    cambios_realizados_log[campo_modelo_db] = valor_recibido
                
                # Procesamiento especial para latitud/longitud
                elif campo_request in ["latitud", "longitud"]:
                    if valor_recibido is not None:
                        try:
                            valor_recibido = float(valor_recibido)
                        except (ValueError, TypeError):
                            logging.warning(f"Valor inválido para {campo_request} para user {current_user.email}: {data[campo_request]}")
                            return jsonify({"error": f"El campo '{campo_request}' debe ser un número decimal válido o nulo."}), 400
                    setattr(current_user, campo_request, valor_recibido)
                    cambios_realizados_log[campo_request] = valor_recibido
                
                # Para otros campos
                else:
                    setattr(current_user, campo_request, valor_recibido)
                    cambios_realizados_log[campo_request] = valor_recibido

        if not cambios_realizados_log:
            return jsonify({"mensaje": "No se proporcionaron datos para actualizar o los campos no son modificables."}), 200

        db.session.commit()
        logging.info(f"✅ PERFIL ACTUALIZADO PARA: {current_user.email} | Cambios: {cambios_realizados_log}")
        return jsonify({"mensaje": "Perfil actualizado correctamente"}), 200

    except Exception as e:
        logging.error(f"❌ ERROR EN PUT /perfil para user {current_user.email}:\n{traceback.format_exc()}", exc_info=True)
        db.session.rollback() # Importante hacer rollback en caso de error después de intentar modificar
        return jsonify({"error": "Error interno al actualizar el perfil."}), 500
    
# CLI opcional para crear base sin migraciones (NO USAR EN PRODUCCIÓN CON MIGRACIONES)
@auth_bp.cli.command("crear_base")
def crear_base():
    # Esta función es peligrosa si se usa con un sistema de migraciones existente.
    # Solo para desarrollo muy temprano o setups sin migraciones.
    # db.drop_all() # Podrías necesitar esto si quieres recrear todo limpiamente
    db.create_all()
    print("✅ Base creada directamente desde los modelos (¡solo para desarrollo!).")

@auth_bp.route('/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return '', 204  # Opcional, Flask-CORS debería manejarlo, pero así seguro no da 404

    data = request.get_json()
    if not data:
        return jsonify({"error": "Solicitud JSON inválida o vacía."}), 400

    email = data.get("email", "").strip().lower()
    password = data.get("password", "").strip()

    if not email or not password:
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=email).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    return jsonify({
        "token": user.token,
        "email": user.email,
        "name": user.name,
        "plan": user.plan,
        "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas
    })
