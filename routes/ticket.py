from flask import Blueprint, request, jsonify, current_app
from models import MunicipioTicket, PymeTicket, User, TicketComentario, db
from services.ticket_service import servicio_tickets
from .auth import token_requerido
from collections import defaultdict
from functools import wraps

# ----- DECORADOR PARA PERMITIR TOKEN O ANON_ID -----
def anon_o_token_requerido(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        user = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            if token:
                user = User.query.filter_by(token=token).first()
        anon_id = request.headers.get("Anon-Id")
        if not user and not anon_id:
            return jsonify({"error": "No autenticado (ni token ni Anon-Id)."}), 401
        return f(user, anon_id, *args, **kwargs)
    return decorated

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

# ---------- LISTA DE TICKETS (logueado) ----------
@ticket_bp.route('/', methods=['GET'])
@token_requerido
def get_tickets_del_usuario(current_user: User):
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar tickets."}), 404

    try:
        if current_user.rubro.nombre.lower().strip() == 'municipios':
            tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
            tipo = 'municipio'
            def serialize_ticket(t):
                return {
                    "id": t.id,
                    "tipo": tipo,
                    "nro_ticket": t.nro_ticket,
                    "asunto": getattr(t, 'asunto', 'N/A'),
                    "estado": t.estado,
                    "fecha": t.fecha.isoformat(),
                    "categoria": getattr(t, 'categoria', None),
                    "direccion": getattr(t, 'direccion', None),
                    "latitud": getattr(t, 'latitud', None),
                    "longitud": getattr(t, 'longitud', None)
                }
        else:
            if current_user.rubro_id:
                tickets = PymeTicket.query.filter_by(rubro_id=current_user.rubro_id).order_by(PymeTicket.fecha.desc()).all()
            else:
                tickets = PymeTicket.query.filter_by(user_id=current_user.id).order_by(PymeTicket.fecha.desc()).all()
            tipo = 'pyme'
            def serialize_ticket(t):
                return {
                    "id": t.id,
                    "tipo": tipo,
                    "nro_ticket": t.nro_ticket,
                    "asunto": getattr(t, 'asunto', 'N/A'),
                    "estado": t.estado,
                    "fecha": t.fecha.isoformat(),
                    "telefono": getattr(t, 'telefono', None),
                    "email": getattr(t, 'email', None),
                    "dni": getattr(t, 'dni', None),
                    "estado_cliente": getattr(t, 'estado_cliente', None),
                    "categoria": getattr(t, 'categoria', None),
                    "direccion": getattr(t, 'direccion', None),
                    "latitud": getattr(t, 'latitud', None),
                    "longitud": getattr(t, 'longitud', None)
                }
        resultado = [serialize_ticket(t) for t in tickets]
        return jsonify(resultado)
    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

# ---------- DETALLE DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>', methods=['GET'])
@token_requerido
def get_detalle_ticket(current_user: User, tipo: str, ticket_id: int):
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket = db.session.get(TicketModel, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    is_admin_muni = tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    is_dueño = ticket.user_id == current_user.id
    is_admin_pyme = tipo == 'pyme' and current_user.rubro_id and getattr(ticket, 'rubro_id', None) == current_user.rubro_id
    if not (is_admin_muni or is_dueño or is_admin_pyme):
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    detalles = getattr(ticket, 'detalles', '') or ''
    nombre, tel, email = "No especificado", "No especificado", "No especificado"
    direccion = getattr(ticket, 'direccion', None)
    if not direccion:
        direccion = "No especificada"
    if "Nombre:" in detalles: nombre = detalles.split("Nombre:")[1].split("\n")[0].strip()
    if "Teléfono:" in detalles: tel = detalles.split("Teléfono:")[1].split("\n")[0].strip()
    if "Email:" in detalles: email = detalles.split("Email:")[1].split("\n")[0].strip()
    if not getattr(ticket, 'direccion', None) and "Dirección:" in detalles:
        direccion = detalles.split("Dirección:")[1].split("\n")[0].strip()

    comentarios = [
        {"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin}
        for c in ticket.comentarios
    ]

    ticket_data = {
        "id": ticket.id,
        "tipo": tipo,
        "nro_ticket": ticket.nro_ticket,
        "asunto": getattr(ticket, 'asunto', ''),
        "categoria": getattr(ticket, 'categoria', ''),
        "estado": ticket.estado,
        "fecha": ticket.fecha.isoformat(),
        "pregunta": getattr(ticket, 'pregunta', ''),
        "detalles": detalles,
        "comentarios": sorted(comentarios, key=lambda c: c['fecha']),
        "nombre_usuario": nombre,
        "telefono": tel,
        "email": email,
        "direccion": direccion,
        "archivo_url": getattr(ticket, 'archivo_url', None),
        "latitud": getattr(ticket, 'latitud', None),
        "longitud": getattr(ticket, 'longitud', None)
    }
    return jsonify(ticket_data)

# ---------- RESPONDER A TICKET (AGENTE) ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@token_requerido
def responder_a_ticket(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    has_permission_to_respond = False
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_permission_to_respond = True
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        if getattr(ticket_obj, 'rubro_id', None) and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id:
            has_permission_to_respond = True

    if not has_permission_to_respond:
        return jsonify({"error": "No tienes permiso para responder este ticket."}), 403

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo,
        comentario_data={"comentario": data["comentario"], "user_id": current_user.id, "es_admin": True}
    )

    if nuevo_comentario:
        if ticket_obj.estado == "nuevo":
            ticket_obj.estado = "en_proceso"
            db.session.commit()

        comentarios_actualizados = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin} for c in ticket_obj.comentarios]
        ticket_data = {
            "id": ticket_obj.id, "tipo": tipo, "nro_ticket": ticket_obj.nro_ticket,
            "asunto": getattr(ticket_obj, 'asunto', ''), "estado": ticket_obj.estado,
            "fecha": ticket_obj.fecha.isoformat(),
            "detalles": getattr(ticket_obj, 'detalles', getattr(ticket_obj, 'pregunta', '')),
            "comentarios": sorted(comentarios_actualizados, key=lambda c: c['fecha']),
            "rubro_id": getattr(ticket_obj, 'rubro_id', None),
            "telefono": getattr(ticket_obj, 'telefono', None),
            "email": getattr(ticket_obj, 'email', None),
            "dni": getattr(ticket_obj, 'dni', None),
            "estado_cliente": getattr(ticket_obj, 'estado_cliente', None),
            "archivo_url": getattr(ticket_obj, 'archivo_url', None),
            "latitud": getattr(ticket_obj, 'latitud', None),
            "longitud": getattr(ticket_obj, 'longitud', None)
        }
        return jsonify(ticket_data), 200

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- CAMBIAR ESTADO DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/estado', methods=['PUT'])
@token_requerido
def cambiar_estado_ticket(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json()
    nuevo_estado = data.get("estado")
    if not nuevo_estado:
        return jsonify({"error": "Falta el nuevo estado."}), 400

    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    has_permission_to_change_state = False
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_permission_to_change_state = True
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        if getattr(ticket_obj, 'rubro_id', None) and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id:
            has_permission_to_change_state = True

    if not has_permission_to_change_state:
        return jsonify({"error": "No tienes permiso para cambiar el estado de este ticket."}), 403

    ticket_obj.estado = nuevo_estado
    db.session.commit()

    comentarios = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin} for c in ticket_obj.comentarios]
    ticket_data = {
        "id": ticket_obj.id, "tipo": tipo, "nro_ticket": ticket_obj.nro_ticket,
        "asunto": getattr(ticket_obj, 'asunto', ''), "estado": ticket_obj.estado,
        "fecha": ticket_obj.fecha.isoformat(),
        "detalles": getattr(ticket_obj, 'detalles', getattr(ticket_obj, 'pregunta', '')),
        "comentarios": sorted(comentarios, key=lambda c: c['fecha']),
        "rubro_id": getattr(ticket_obj, 'rubro_id', None),
        "telefono": getattr(ticket_obj, 'telefono', None),
        "email": getattr(ticket_obj, 'email', None),
        "dni": getattr(ticket_obj, 'dni', None),
        "estado_cliente": getattr(ticket_obj, 'estado_cliente', None),
        "archivo_url": getattr(ticket_obj, 'archivo_url', None)
        ,"latitud": getattr(ticket_obj, 'latitud', None)
        ,"longitud": getattr(ticket_obj, 'longitud', None)
    }
    return jsonify(ticket_data)

# ---------- CHAT EN VIVO: MENSAJES (TOKEN O ANONIMO) ----------
@ticket_bp.route('/chat/<int:ticket_id>/mensajes', methods=['GET'])
@anon_o_token_requerido
def get_chat_mensajes(current_user, anon_id, ticket_id):
    """
    Devuelve los mensajes del chat en vivo.
    Permite acceso por user logueado o por anon_id (controla que corresponda).
    """
    try:
        sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        es_agente_municipal = current_user and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
        es_dueño_del_ticket = current_user and sala_de_chat.user_id == current_user.id
        es_anonimo_ticket = anon_id and sala_de_chat.anon_id == anon_id

        if not (es_agente_municipal or es_dueño_del_ticket or es_anonimo_ticket):
            return jsonify({"error": "No tienes permiso para acceder a este chat."}), 403

        ultimo_mensaje_id = request.args.get('ultimo_mensaje_id', default=0, type=int)
        mensajes_nuevos = (
            TicketComentario.query
            .filter(
                TicketComentario.municipio_ticket_id == ticket_id,
                TicketComentario.id > ultimo_mensaje_id
            )
            .order_by(TicketComentario.fecha.asc())
            .all()
        )
        mensajes_formateados = [
            {
                "id": msg.id,
                "texto": msg.comentario,
                "fecha": msg.fecha.isoformat(),
                "es_admin": msg.es_admin
            }
            for msg in mensajes_nuevos
        ]
        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados
        }
        return jsonify(respuesta_final)
    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500

# ---------- CHAT EN VIVO: RESPONDER CIUDADANO (TOKEN O ANONIMO) ----------
@ticket_bp.route('/chat/<int:ticket_id>/responder_ciudadano', methods=['POST'])
@anon_o_token_requerido
def responder_ciudadano_a_chat(current_user, anon_id, ticket_id):
    """
    Permite al ciudadano responder en el chat de su ticket (token o anon).
    """
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
    if not sala_de_chat:
        return jsonify({"error": "Sala de chat no encontrada."}), 404

    if current_user:
        es_dueño = sala_de_chat.user_id == current_user.id
    else:
        es_dueño = False
    es_anonimo_ticket = anon_id and sala_de_chat.anon_id == anon_id

    if not (es_dueño or es_anonimo_ticket):
        return jsonify({"error": "No tienes permiso para responder en este chat."}), 403

    user_id_para_comentario = current_user.id if current_user else None

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id,
        tipo_ticket="municipio",
        comentario_data={
            "comentario": data["comentario"],
            "user_id": user_id_para_comentario,
            "es_admin": False
        }
    )
    if nuevo_comentario:
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- PANEL POR CATEGORÍA (AGENTES MUNICIPALES) ----------
@ticket_bp.route('/panel_por_categoria', methods=['GET'])
@token_requerido
def get_panel_por_categoria(current_user: User):
    es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    if not es_agente_municipal:
        return jsonify({"error": "No tienes permiso para acceder a este panel."}), 403

    try:
        tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
        tickets_agrupados = defaultdict(list)

        for ticket in tickets:
            direccion = ticket.direccion or "No especificada"
            if not ticket.direccion and ticket.detalles:
                for line in ticket.detalles.splitlines():
                    if "Dirección del problema:" in line:
                        direccion = line.split("Dirección del problema:")[1].strip()
                        break

            ticket_data = {
                "id": ticket.id,
                "tipo": "municipio",
                "nro_ticket": ticket.nro_ticket,
                "asunto": ticket.asunto,
                "estado": ticket.estado,
                "fecha": ticket.fecha.isoformat(),
                "direccion": direccion,
                "latitud": getattr(ticket, 'latitud', None),
                "longitud": getattr(ticket, 'longitud', None)
            }
            tickets_agrupados[ticket.categoria or "Sin Categoría"].append(ticket_data)

        return jsonify(tickets_agrupados)

    except Exception as e:
        current_app.logger.error(f"Error en get_panel_por_categoria: {e}", exc_info=True)
        return jsonify({"error": "Error interno al generar el panel de tickets."}), 500


# ---------- ACTUALIZAR UBICACIÓN DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/ubicacion', methods=['PUT'])
@token_requerido
def actualizar_ubicacion_ticket(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json() or {}
    lat = data.get('latitud')
    lon = data.get('longitud')
    direccion = data.get('direccion')

    TicketModel = MunicipioTicket if tipo == 'municipio' else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    has_perm = False
    if current_user.id == ticket_obj.user_id:
        has_perm = True
    elif tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_perm = True
    elif tipo == 'pyme' and current_user.rubro_id and getattr(ticket_obj, 'rubro_id', None) == current_user.rubro_id:
        has_perm = True

    if not has_perm:
        return jsonify({"error": "No tienes permiso para modificar este ticket."}), 403

    if lat is not None:
        ticket_obj.latitud = lat
    if lon is not None:
        ticket_obj.longitud = lon
    if direccion:
        ticket_obj.direccion = direccion

    db.session.commit()

    return jsonify({
        "id": ticket_obj.id,
        "latitud": ticket_obj.latitud,
        "longitud": ticket_obj.longitud,
        "direccion": ticket_obj.direccion
    })
