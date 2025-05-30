# En routes/auth.py
from flask import Blueprint, request, jsonify, current_app
from werkzeug.security import check_password_hash, generate_password_hash
from models import User, Rubro, CatalogoItem # CatalogoItem se usa en /debug/users
from extensions import db
from functools import wraps
import uuid
import logging # Asegúrate que esté importado
import traceback
import json # Para validar horario_json

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
            logging.warning(f"Intento de acceso con token inválido: {token[:15]}...") # Loguear solo parte del token
            return jsonify({"error": "Token inválido o sesión expirada"}), 403 # 403 es más apropiado para token inválido

        return f(user, *args, **kwargs)
    return decorated

# Obtener información del usuario actual
@auth_bp.route('/me', methods=['GET'])
@token_requerido # Proteger /me también
def get_current_user(user): # user es inyectado por @token_requerido
    try:
        rubro_nombre = "General"
        if user.rubro_id:
            rubro = db.session.get(Rubro, user.rubro_id) # Más eficiente para búsqueda por PK
            if rubro:
                rubro_nombre = rubro.nombre

        # Usar getattr para todos los campos que podrían ser None o no existir, con defaults claros
        user_data_response = {
            "token": user.token,
            "id": user.id,
            "name": getattr(user, "name", "") or "", # Devolver string vacío si es None
            "email": getattr(user, "email", "") or "",
            "plan": getattr(user, "plan", "gratis") or "gratis",
            "preguntas_usadas": getattr(user, "preguntas_usadas", 0) or 0,
            "limite_preguntas": getattr(user, "limite_preguntas", 50) or 50,
            "nombre_empresa": getattr(user, "nombre_empresa", "") or "",
            "telefono": getattr(user, "telefono", "") or "",
            "direccion": getattr(user, "direccion", "") or "",
            "ciudad": getattr(user, "ciudad", "") or "",
            "provincia": getattr(user, "provincia", "") or "", # Asumiendo que 'provincia' es el campo actual
            "pais": getattr(user, "pais", "") or "",
            "latitud": getattr(user, "latitud", None), # Mantener None si no existe
            "longitud": getattr(user, "longitud", None), # Mantener None si no existe
            "link_web": getattr(user, "link_web", "") or "",
            "horario_json": getattr(user, "horario_json", '[]') or '[]', # Devolver '[]' como string si es None o vacío
            "logo_url": getattr(user, "logo_url", "") or "",
            # "ubicacion": getattr(user, "ubicacion", "") or "", # Considera si este campo es redundante
            "rubro": rubro_nombre
        }
        return jsonify(user_data_response)
    except Exception as e:
        # Usar current_app.logger si está configurado y disponible, sino el logging estándar
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
            # Asumiendo que CatalogoItem tiene una relación con User
            # items = u.catalogo_items # Si la relación está bien definida
            items = CatalogoItem.query.filter_by(user_id=u.id).all() # Tu forma actual
            catalogo_data = [{
                "nombre": item.nombre, "descripcion": item.descripcion, "precio": item.precio,
                "cantidad": item.cantidad, "categoria": item.categoria or "", "unidad": item.unidad or ""
            } for item in items]
            resultado.append({
                "id": u.id, "token": u.token, "name": u.name, "email": u.email,
                "empresa": u.nombre_empresa, "rubro": rubro.nombre if rubro else "General",
                "telefono": u.telefono, "direccion": u.direccion, "link_web": u.link_web,
                "horario_simple": u.horario, # El string simple de horario
                "horario_estructurado": u.horario_json, # El JSON de horarios
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
        email = data.get("email", "").strip().lower() # Normalizar email
        password = data.get("password", "").strip()
        nombre_empresa = data.get("nombre_empresa", "").strip()
        rubro_nombre = data.get("rubro", "").strip() # Frontend envía nombre, backend busca ID

        if not all([name, email, password, nombre_empresa, rubro_nombre]):
            return jsonify({"error": "Todos los campos son obligatorios: nombre, email, contraseña, nombre de empresa y rubro."}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Ya existe un usuario con ese correo electrónico."}), 409 # 409 Conflict

        rubro = Rubro.query.filter(func.lower(Rubro.nombre) == func.lower(rubro_nombre)).first() # Búsqueda case-insensitive
        if not rubro:
            return jsonify({"error": f"El rubro '{rubro_nombre}' no es válido o no se encontró."}), 400

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password), # Usar password_hash, no set_password si este es el constructor
            token=str(uuid.uuid4()),
            nombre_empresa=nombre_empresa,
            rubro_id=rubro.id,
            plan="gratis", # Default plan
            preguntas_usadas=0,
            limite_preguntas=50 # Default para plan gratis
            # Los campos de dirección y horario se llenarán en el perfil
        )
        # user.set_password(password) # Si tienes un método set_password en el modelo User

        db.session.add(user)
        db.session.commit()
        logging.info(f"✅ Usuario registrado: {email} con ID {user.id}")

        return jsonify({
            "mensaje": "Usuario registrado exitosamente.", # Mensaje de éxito
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
        }), 201 # 201 Created
    except Exception as e:
        logging.error(f"❌ Error en /register:\n{traceback.format_exc()}")
        return jsonify({"error": "Error interno al registrar el usuario."}), 500

# Login de usuario
@auth_bp.route('/login', methods=['POST'])
def login():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Solicitud JSON inválida o vacía."}), 400
            
        email = data.get("email", "").strip().lower() # Normalizar email
        password = data.get("password", "").strip()

        if not email or not password:
            return jsonify({"error": "Correo electrónico y contraseña son requeridos."}), 400

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password): # Asumiendo que check_password es un método en tu modelo User
            logging.warning(f"❌ Intento fallido de login para email: {email}")
            return jsonify({"error": "Credenciales inválidas."}), 401

        logging.info(f"✅ Usuario logueado: {email}")
        return jsonify({
            "token": user.token,
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "plan": user.plan,
            "preguntas_usadas": user.preguntas_usadas or 0,
            "limite_preguntas": user.limite_preguntas or 0
        })
    except Exception as e:
        logging.error(f"❌ Error en /login:\n{traceback.format_exc()}")
        return jsonify({"error": "Error interno al iniciar sesión."}), 500

# Actualizar perfil del usuario
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_profile(current_user): # Renombrado a current_user para claridad, inyectado por @token_requerido
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "No se recibieron datos válidos en formato JSON."}), 400

        logging.info(f"📥 DATOS RECIBIDOS en PUT /perfil para user {current_user.email}: {data}")

        # Campos permitidos para actualizar y sus tipos esperados (opcional, para validación más estricta)
        # (nombre_campo_modelo, tipo_esperado, es_json_string)
        campos_info = {
            "nombre_empresa": (str, False), "telefono": (str, False), "direccion": (str, False),
            "ciudad": (str, False), "provincia": (str, False), "pais": (str, False),
            "latitud": (float, False), "longitud": (float, False),
            "link_web": (str, False), "horario_json": (str, True), # horario_json se espera como string JSON
            "logo_url": (str, False)
            # "ubicacion": (str, False), # Decide si este campo se mantiene
        }
        
        cambios_realizados_log = {}

        for campo_modelo, (tipo_esperado, es_json) in campos_info.items():
            if campo_modelo in data:
                valor_recibido = data[campo_modelo]
                
                # Manejar strings vacíos como None para campos opcionales (ej. latitud, longitud, logo_url)
                # o si quieres permitir borrar un valor.
                if isinstance(valor_recibido, str):
                    valor_recibido = valor_recibido.strip()
                    if valor_recibido == "":
                        valor_recibido = None 
                
                # Validar y procesar horario_json
                if campo_modelo == "horario_json":
                    if valor_recibido is not None: # Solo si se envió algo (no es None por string vacío)
                        if not isinstance(valor_recibido, str):
                             return jsonify({"error": f"El campo '{campo_modelo}' debe ser un string JSON."}), 400
                        try:
                            parsed_horarios = json.loads(valor_recibido)
                            if not isinstance(parsed_horarios, list):
                                raise ValueError("Debe ser una lista.")
                            for item_horario in parsed_horarios:
                                if not isinstance(item_horario, dict) or \
                                   not all(k in item_horario for k in ["abre", "cierra", "cerrado"]):
                                    raise ValueError("Cada ítem debe ser un dict con 'abre', 'cierra', 'cerrado'.")
                            # Si es válido, se guarda el string JSON (valor_recibido)
                        except (json.JSONDecodeError, ValueError) as e_json:
                            logging.warning(f"Formato de {campo_modelo} inválido para user {current_user.email}: {valor_recibido}. Error: {e_json}")
                            return jsonify({"error": f"El formato de '{campo_modelo}' es inválido."}), 400
                # Validar y convertir latitud/longitud
                elif campo_modelo in ["latitud", "longitud"]:
                    if valor_recibido is not None: # Puede ser None si se quiere borrar
                        try:
                            valor_recibido = float(valor_recibido)
                        except (ValueError, TypeError):
                             logging.warning(f"Valor inválido para {campo_modelo} para user {current_user.email}: {data[campo_modelo]}")
                             return jsonify({"error": f"El campo '{campo_modelo}' debe ser un número decimal válido o nulo."}), 400
                
                # Actualizar atributo en el objeto user
                setattr(current_user, campo_modelo, valor_recibido)
                cambios_realizados_log[campo_modelo] = valor_recibido

        if not cambios_realizados_log:
             return jsonify({"mensaje": "No se proporcionaron datos para actualizar o los campos no son modificables."}), 200 # O 304 Not Modified

        db.session.commit()
        logging.info(f"✅ PERFIL ACTUALIZADO PARA: {current_user.email} | Cambios: {cambios_realizados_log}")
        return jsonify({"mensaje": "Perfil actualizado correctamente"}), 200

    except Exception as e:
        logging.error(f"❌ ERROR EN PUT /perfil para user {current_user.email}:\n{traceback.format_exc()}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar el perfil."}), 500
    
# CLI opcional para crear base sin migraciones
@auth_bp.cli.command("crear_base")
def crear_base():
    # Considera usar db.create_all() solo en entornos de desarrollo o para la configuración inicial.
    # Para producción, las migraciones (flask db upgrade) son el método preferido.
    db.create_all()
    print("✅ Base creada directamente desde los modelos (¡solo para desarrollo!).")