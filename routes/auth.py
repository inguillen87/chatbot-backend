# src/routes/auth.py

from flask import Blueprint, request, jsonify, current_app
from sqlalchemy import func
from werkzeug.security import check_password_hash # Importación que faltaba
from models import User, Rubro, db, PymePedido # <-- Importamos PymePedido
from functools import wraps
import uuid
import json
from datetime import datetime

# Definimos el Blueprint con el prefijo /user para todos los endpoints aquí
auth_bp = Blueprint('auth', __name__, url_prefix='/user') # <-- Prefijo /user

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
# La ruta real será /user/login debido al url_prefix del Blueprint
@auth_bp.route('/login', methods=['POST'])
def login():
    data = request.get_json()
    if not data or not data.get('email') or not data.get('password'):
        return jsonify({"error": "Email y contraseña requeridos."}), 400

    user = User.query.filter_by(email=data.get("email").strip().lower()).first()

    if not user or not user.check_password(data.get("password")):
        current_app.logger.warning(f"Intento de login fallido para el email: {data.get('email')}")
        return jsonify({"error": "Email o contraseña incorrectos."}), 401

    # Asegurarse de que el usuario tenga un token si no lo tiene (para compatibilidad)
    if not user.token:
        user.token = str(uuid.uuid4())
        db.session.commit() # Guardar el nuevo token

    current_app.logger.info(f"Login exitoso para: {user.email}")
    return jsonify({
        "mensaje": "Login exitoso",
        "id": user.id,
        "token": user.token,
        "email": user.email,
        "name": user.name,
    })

# --- ENDPOINT DE REGISTRO ---
# La ruta real será /user/register debido al url_prefix del Blueprint
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
        token=str(uuid.uuid4()), # Generar un token al registrarse
        nombre_empresa=data['nombre_empresa'].strip(),
        rubro_id=rubro.id,
        rubro=rubro, # Asignar el objeto rubro si la relación lo permite
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
# La ruta real será /user/me debido al url_prefix del Blueprint
@auth_bp.route('/me', methods=['GET'])
@token_requerido
def get_user_profile(current_user: User):
    """
    Obtiene la información del perfil del usuario logueado.
    """
    try:
        horario_json_data = None
        if current_user.horario:
            try:
                horario_json_data = json.loads(current_user.horario)
            except json.JSONDecodeError:
                current_app.logger.error(f"Error decodificando horario JSON para user {current_user.id}")
                horario_json_data = None

        rubro_data = {"id": current_user.rubro.id, "nombre": current_user.rubro.nombre} if current_user.rubro else None

        return jsonify({
            "id": current_user.id,
            "name": current_user.name,
            "email": current_user.email,
            "nombre_empresa": current_user.nombre_empresa,
            "direccion": current_user.direccion,
            "ciudad": current_user.ciudad,
            "provincia": current_user.provincia,
            "pais": current_user.pais,
            "latitud": current_user.latitud,
            "longitud": current_user.longitud,
            "telefono": current_user.telefono,
            "link_web": current_user.link_web,
            "acepto_terminos": current_user.acepto_terminos,
            "fecha_aceptacion_terminos": current_user.fecha_aceptacion_terminos.isoformat() if current_user.fecha_aceptacion_terminos else None,
            "horario_json": horario_json_data,
            "plan": current_user.plan,
            "preguntas_usadas": current_user.preguntas_usadas,
            "limite_preguntas": current_user.limite_preguntas,
            "last_reset": current_user.last_reset.isoformat() if current_user.last_reset else None,
            "rubro": rubro_data, # Devolver el objeto rubro
            "logo_url": getattr(current_user, "logo_url", "") # Asegurando un valor por defecto si no existe el atributo
        }), 200
    except Exception as e:
        current_app.logger.error(f"Error al obtener perfil para user {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al obtener perfil"}), 500

# --- ENDPOINT PARA ACTUALIZAR PERFIL ---
# La ruta real será /user/perfil debido al url_prefix del Blueprint
@auth_bp.route('/perfil', methods=['PUT'])
@token_requerido
def update_user_profile(current_user: User):
    """
    Actualiza la información del perfil del usuario logueado.
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "No se recibieron datos."}), 400

    try:
        current_user.nombre_empresa = data.get('nombre_empresa', current_user.nombre_empresa)
        current_user.telefono = data.get('telefono', current_user.telefono)
        current_user.direccion = data.get('direccion', current_user.direccion)
        current_user.ciudad = data.get('ciudad', current_user.ciudad)
        current_user.provincia = data.get('provincia', current_user.provincia)
        current_user.pais = data.get('pais', current_user.pais)
        current_user.latitud = data.get('latitud', current_user.latitud)
        current_user.longitud = data.get('longitud', current_user.longitud)
        current_user.link_web = data.get('link_web', current_user.link_web)
        
        # Horario JSON
        horario_json_str = data.get('horario_json')
        if horario_json_str is not None: # Si se envía, puede ser un string vacío
            try:
                # Valida que sea un JSON válido antes de guardar si no está vacío
                if horario_json_str:
                    json.loads(horario_json_str) 
                current_user.horario = horario_json_str
            except json.JSONDecodeError:
                current_app.logger.error(f"Horario JSON inválido para user {current_user.id}")
                return jsonify({"error": "Formato de horario JSON inválido"}), 400

        # Si el logo_url se envía (puede ser None para borrarlo)
        if 'logo_url' in data:
            current_user.logo_url = data['logo_url']
            
        db.session.commit()
        return jsonify({"mensaje": "Perfil actualizado exitosamente"}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar perfil para user {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al actualizar perfil"}), 500

# --- ENDPOINT /USER/PEDIDOS (Listar Pedidos de la PYME Logueada) ---
# La ruta real será /user/pedidos debido al url_prefix del Blueprint
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
        # Filtra los pedidos por el rubro de la PYME logueada.
        # Esto incluye pedidos de clientes anónimos que se hayan asociado a este rubro.
        pedidos = PymePedido.query.filter_by(rubro=rubro_nombre).order_by(PymePedido.fecha.desc()).all()

        pedidos_data = []
        for pedido in pedidos:
            detalles_parsed = [] # Inicializamos como lista vacía por defecto
            if pedido.detalles:
                try:
                    # Intentar parsear el JSON de detalles
                    parsed_content = json.loads(pedido.detalles)
                    # Si es una lista, usarla directamente
                    if isinstance(parsed_content, list):
                        detalles_parsed = parsed_content
                    # Si es un diccionario (o cualquier otra cosa), encapsularlo para mostrar
                    else:
                        detalles_parsed = [{"info": parsed_content}] # Para asegurar que siempre sea iterable en el frontend
                except json.JSONDecodeError:
                    current_app.logger.warning(f"Detalles de pedido {pedido.nro_pedido} mal formados: {pedido.detalles}")
                    detalles_parsed = [{"error": "Detalles del pedido mal formados o no son un array de productos"}] 
            
            pedidos_data.append({
                "id": pedido.id,
                "nro_pedido": pedido.nro_pedido,
                "asunto": pedido.asunto,
                "estado": pedido.estado,
                "detalles": detalles_parsed, # Devuelve los detalles como objeto/lista
                "monto_total": pedido.monto_total,
                "fecha_creacion": pedido.fecha.isoformat(), # Formato ISO para fácil parseo en JS
                "nombre_cliente": pedido.nombre_cliente,
                "email_cliente": pedido.email_cliente,
                "telefono_cliente": pedido.telefono_cliente,
                "rubro": pedido.rubro
            })
        
        return jsonify({"pedidos": pedidos_data}), 200

    except Exception as e:
        current_app.logger.error(f"Error al obtener pedidos para user {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al obtener pedidos"}), 500

# --- ENDPOINT PARA ACTUALIZAR ESTADO DE UN PEDIDO ---
# La ruta real será /user/pedidos/<nro_pedido>/estado debido al url_prefix del Blueprint
@auth_bp.route('/pedidos/<string:nro_pedido>/estado', methods=['PUT'])
@token_requerido
def update_pedido_estado(nro_pedido):
    """
    Actualiza el estado de un pedido específico.
    Solo la PYME dueña del rubro asociado al pedido puede actualizarlo.
    """
    user_id = current_user.id # ID del usuario PYME que hace la petición
    data = request.get_json()
    new_estado = data.get('estado')

    if not new_estado:
        return jsonify({"error": "Estado no proporcionado"}), 400
    
    try:
        # Verificar que el usuario tenga un rubro para poder gestionar pedidos
        if not current_user.rubro:
            return jsonify({"error": "Usuario sin rubro asociado para gestionar pedidos"}), 403

        # Busca el pedido por número y asegúrate de que pertenezca al rubro de la PYME logueada.
        pedido = PymePedido.query.filter_by(nro_pedido=nro_pedido, rubro=current_user.rubro.nombre.lower()).first()

        if not pedido:
            return jsonify({"error": "Pedido no encontrado o no autorizado. Verifique si pertenece a su rubro."}), 404
        
        # Validar que el nuevo estado sea uno permitido
        allowed_states = ["pendiente", "en_proceso", "enviado", "entregado", "cancelado", "satisfecho"] 
        if new_estado not in allowed_states:
            return jsonify({"error": f"Estado '{new_estado}' no permitido"}), 400

        pedido.estado = new_estado
        db.session.commit()
        logger.info(f"Estado del pedido {nro_pedido} (user {user_id}) actualizado a: {new_estado}")
        
        # Opcional: Podrías devolver el pedido actualizado completo si el frontend lo necesita
        return jsonify({"message": "Estado del pedido actualizado con éxito", "pedido": {"nro_pedido": pedido.nro_pedido, "estado": pedido.estado}}), 200

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar estado del pedido {nro_pedido} para user {user_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno del servidor al actualizar estado del pedido"}), 500

# --- ENDPOINTS DE CATÁLOGO (Mantener si los tienes en este archivo o añadir aquí) ---
# Ejemplo de endpoints de catálogo si están en auth.py
# @auth_bp.route('/subir_catalogo', methods=['POST'])
# @token_requerido
# def upload_catalog(current_user: User):
#    # Lógica para subir catálogo (ej. desde Perfil.tsx)
#    pass

# @auth_bp.route('/get_catalog_items', methods=['GET'])
# @token_requerido
# def get_catalog_items(current_user: User):
#    # Lógica para obtener items de catálogo
#    pass