# src/routes/auth.py

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func
from werkzeug.security import check_password_hash # Importación que faltaba
from models import User, Rubro, db, PymePedido # <-- Importamos PymePedido
from functools import wraps
import uuid
import json
from datetime import datetime

# Definimos el Blueprint. NO le daremos url_prefix aquí para no romper /login, /register, etc.
auth_bp = Blueprint('auth', __name__) 

def token_requerido(f):
    """
    Decorador para proteger rutas, asegurando que un token válido esté presente
    en el encabezado 'Authorization' y que corresponda a un usuario activo.
    Pasa el objeto `User` al handler de la ruta.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization")
        token = None
        
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        
        if not token:
            current_app.logger.warning("Intento de acceso a ruta protegida sin token.")
            return jsonify({"error": "Token faltante o malformado"}), 401

        user = User.query.filter_by(token=token).first()

        if not user:
            current_app.logger.warning(f"Intento de acceso con token inválido o expirado: {token}")
            return jsonify({"error": "Token inválido o sesión expirada"}), 401

        # Pasa el objeto `user` directamente a la función decorada
        return f(user, *args, **kwargs)
    return decorated

# --- ENDPOINT DE LOGIN ---
# La ruta será /login
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=data.get("email").strip().lower()).first()

    if not user or not user.check_password(data.get("password")):
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    # Asegurarse de que el usuario tenga un token si no lo tiene
    if not user.token:
        user.token = str(uuid.uuid4())
        db.session.commit()

    current_app.logger.info(f"Login exitoso para: {user.email}")
    return jsonify({
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": user.token,
        "email": user.email,
        "name": user.name,
    })

# --- ENDPOINT DE REGISTRO ---
# La ruta será /register
@auth_bp.route('/register', methods=['POST'])
def register():
    data = request.get_json()
    if not data:
        return jsonify({"error": "Solicitud JSON inválida."}), 400

    required_fields = ["name", "email", "password", "nombre_empresa", "rubro", "acepto_terminos"]
    if not all(field in data and data[field] for field in required_fields):
        return jsonify({"error": "Todos los campos son obligatorios."}), 400

    if not data["acepto_terminos"]:
        return jsonify({"error": "Es necesario aceptar los términos y condiciones."}), 400
        
    if User.query.filter_by(email=data['email'].strip().lower()).first():
        return jsonify({"error": "Ya existe un usuario con ese correo electrónico."}), 409

    rubro = Rubro.query.filter(func.lower(Rubro.nombre) == func.lower(data['rubro'].strip())).first()
    if not rubro:
        return jsonify({"error": f"El rubro '{data['rubro']}' no es válido."}), 400

    user = User(
        name=data['name'].strip(),
        email=data['email'].strip().lower(),
        token=str(uuid.uuid4()),
        nombre_empresa=data['nombre_empresa'].strip(),
        rubro_id=rubro.id,
        rubro=rubro,
        plan="gratis",
        acepto_terminos=True,
        fecha_aceptacion_terminos=datetime.utcnow()
    )
    user.set_password(data['password'])

    try:
        db.session.add(user)
        db.session.commit()
        current_app.logger.info(f"Usuario registrado: {user.email} con ID {user.id}")
        return jsonify({
            "mensaje": "Usuario registrado exitosamente.",
            "id": user.id,
            "token": user.token,
            "name": user.name,
            "email": user.email,
        }), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al registrar usuario: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el usuario."}), 500

# --- ENDPOINT /ME (Obtener perfil del usuario logueado) ---
# La ruta será /me
@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_current_user(user):
    rubro_nombre = user.rubro.nombre if user.rubro else "General"
    return jsonify({
        "id": user.id, "email": user.email, "name": user.name, "token": user.token,
        "rubro": rubro_nombre, "nombre_empresa": user.nombre_empresa, "telefono": user.telefono,
        "direccion": user.direccion, "ciudad": user.ciudad, "provincia": user.provincia,
        "pais": user.pais, "latitud": user.latitud, "longitud": user.longitud,
        "link_web": user.link_web, "plan": user.plan, "preguntas_usadas": user.preguntas_usadas,
        "limite_preguntas": user.limite_preguntas, "horario_json": user.horario_json,
        "logo_url": getattr(user, "logo_url", ""),
    })

# --- ENDPOINT PARA ACTUALIZAR PERFIL ---
# La ruta será /perfil
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def actualizar_perfil(user):
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    try:
        for key, value in data.items():
            if hasattr(user, key):
                if key == "horario_json":
                    setattr(user, "horario", value)
                else:
                    setattr(user, key, value)
        
        # Horario JSON
        horario_json_str = data.get('horario_json')
        if horario_json_str is not None:
            try:
                if horario_json_str:
                    json.loads(horario_json_str) 
                user.horario = horario_json_str
            except json.JSONDecodeError:
                current_app.logger.error(f"Horario JSON inválido para user {user.id}")
                return jsonify({"error": "Formato de horario JSON inválido"}), 400

        if 'logo_url' in data:
            user.logo_url = data['logo_url']
            
        db.session.commit()
        return jsonify({"mensaje": "Perfil actualizado correctamente."}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar perfil para {user.email}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar el perfil."}), 500

# --- NUEVOS ENDPOINTS DE PEDIDOS (AHORA EN auth.py) ---

# ENDPOINT /USER/PEDIDOS (Listar Pedidos de la PYME Logueada)
# La ruta real será /user/pedidos
@auth_bp.route('/pedidos', methods=['GET'])
@token_requerido
def get_user_pedidos(current_user: User):
    """
    Obtiene la lista de pedidos asociados a la PYME (current_user).
    La PYME ve los pedidos de su rubro.
    """
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar pedidos."}), 404

    try:
        rubro_nombre = current_user.rubro.nombre.lower()
        pedidos = PymePedido.query.filter_by(rubro=rubro_nombre).order_by(PymePedido.fecha.desc()).all()

        pedidos_data = []
        for pedido in pedidos:
            detalles_parsed = [] 
            if pedido.detalles:
                try:
                    parsed_content = json.loads(pedido.detalles)
                    if isinstance(parsed_content, list):
                        detalles_parsed = parsed_content
                    else: # Si no es lista, encapsularlo para mostrar como info genérica
                        detalles_parsed = [{"info": parsed_content}] 
                except json.JSONDecodeError:
                    current_app.logger.warning(f"Detalles de pedido {pedido.nro_pedido} mal formados: {pedido.detalles}")
                    detalles_parsed = [{"error": "Detalles del pedido mal formados o no son un array de productos"}] 
            
            pedidos_data.append({
                "id": pedido.id,
                "nro_pedido": pedido.nro_pedido,
                "asunto": pedido.asunto,
                "estado": pedido.estado,
                "detalles": detalles_parsed, 
                "monto_total": pedido.monto_total,
                "fecha_creacion": pedido.fecha.isoformat(), 
                "nombre_cliente": pedido.nombre_cliente,
                "email_cliente": pedido.email_cliente,
                "telefono_cliente": pedido.telefono_cliente,
                "rubro": pedido.rubro
            })
        
        return jsonify({"pedidos": pedidos_data}), 200

    except Exception as e:
        current_app.logger.error(f"Error al obtener pedidos para user {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al obtener pedidos"}), 500

# ENDPOINT PARA ACTUALIZAR ESTADO DE UN PEDIDO
# La ruta real será /user/pedidos/<nro_pedido>/estado
@auth_bp.route('/pedidos/<string:nro_pedido>/estado', methods=['PUT'])
@token_requerido
def update_pedido_estado(nro_pedido):
    """
    Actualiza el estado de un pedido específico.
    Solo la PYME dueña del rubro asociado al pedido puede actualizarlo.
    """
    user_id = current_user.id 
    data = request.get_json()
    new_estado = data.get('estado')

    if not new_estado:
        return jsonify({"error": "Estado no proporcionado"}), 400
    
    try:
        if not current_user.rubro:
            return jsonify({"error": "Usuario sin rubro asociado para gestionar pedidos"}), 403

        pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido, rubro=current_user.rubro.nombre.lower()).first()

        if not pedido:
            return jsonify({"error": "Pedido no encontrado o no autorizado. Verifique si pertenece a su rubro."}), 404
        
        allowed_states = ["pendiente", "en_proceso", "enviado", "entregado", "cancelado", "satisfecho"] 
        if new_estado not in allowed_states:
            return jsonify({"error": f"Estado '{new_estado}' no permitido"}), 400

        pedido.estado = new_estado
        db.session.commit()
        logger.info(f"Estado del pedido {nro_pedido} (user {user_id}) actualizado a: {new_estado}")
        
        return jsonify({"message": "Estado del pedido actualizado con éxito", "pedido": {"nro_pedido": pedido.nro_pedido, "estado": pedido.estado}}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar estado del pedido {nro_pedido} para user {user_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al actualizar estado del pedido"}), 500

# --- ENDPOINTS DE CATÁLOGO (Mantener si los tienes en este archivo o añadir aquí) ---
# Si tus endpoints de catálogo están aquí, pégalos.
# Por ejemplo:
# @auth_bp.route('/subir_catalogo', methods=['POST'])
# @token_requerido
# def upload_catalog(current_user: User):
#    # ... (Tu lógica existente para subir catálogo)
#    pass

# @auth_bp.route('/get_catalog_items', methods=['GET'])
# @token_requerido
# def get_catalog_items(current_user: User):
#    # ... (Tu lógica para obtener items de catálogo)
#    pass