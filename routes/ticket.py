from flask import Blueprint, request, jsonify, current_app
from models import MunicipioTicket, PymeTicket, User, TicketComentario, db
from services.ticket_service import servicio_tickets
from .auth import token_requerido
from collections import defaultdict

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

# ... (código anterior)

@ticket_bp.route('/', methods=['GET'])
@token_requerido
def get_tickets_del_usuario(current_user: User):
    """
    Lista los tickets para el usuario logueado (municipio o pyme).
    """
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar tickets."}), 404

    try:
        # CAMBIO: Usar 'municipio' en la comparación
        if current_user.rubro.nombre.lower().strip() == 'municipios': # Esta línea se mantiene para chequear el rol del usuario
            tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
            tipo = 'municipio' # <--- Asegúrate que aquí sea 'municipio'
            def serialize_ticket(t):
                return {
                    "id": t.id,
                    "tipo": tipo, # Esto será 'municipio'
                    "nro_ticket": t.nro_ticket,
                    "asunto": getattr(t, 'asunto', 'N/A'),
                    "estado": t.estado,
                    "fecha": t.fecha.isoformat(),
                    "categoria": getattr(t, 'categoria', None)
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
                    "categoria": getattr(t, 'categoria', None)
                }
        resultado = [serialize_ticket(t) for t in tickets]
        return jsonify(resultado)
    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500


@ticket_bp.route('/<string:tipo>/<int:ticket_id>', methods=['GET'])
@token_requerido
def get_detalle_ticket(current_user: User, tipo: str, ticket_id: int):
    """
    Devuelve el detalle completo de UN ticket, incluyendo comentarios y datos de contacto.
    """
    # CAMBIO: Usar 'municipio' para seleccionar el modelo
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket # <--- CAMBIO AQUÍ
    ticket = db.session.get(TicketModel, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # CAMBIO: Usar 'municipio' en la comparación de permisos
    is_admin_muni = tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios' # <--- CAMBIO AQUÍ
    is_dueño = ticket.user_id == current_user.id
    is_admin_pyme = tipo == 'pyme' and current_user.rubro_id and getattr(ticket, 'rubro_id', None) == current_user.rubro_id
    if not (is_admin_muni or is_dueño or is_admin_pyme):
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    detalles = getattr(ticket, 'detalles', '') or ''
    nombre, tel, email, direccion = "No especificado", "No especificado", "No especificado", "No especificada"
    if "Nombre:" in detalles: nombre = detalles.split("Nombre:")[1].split("\n")[0].strip()
    if "Teléfono:" in detalles: tel = detalles.split("Teléfono:")[1].split("\n")[0].strip()
    if "Email:" in detalles: email = detalles.split("Email:")[1].split("\n")[0].strip()
    if "Dirección:" in detalles: direccion = detalles.split("Dirección:")[1].split("\n")[0].strip()

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
        "archivo_url": getattr(ticket, 'archivo_url', None)
    }
    return jsonify(ticket_data)

@ticket_bp.route('/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@token_requerido
def responder_a_ticket(current_user: User, tipo: str, ticket_id: int):
    """Permite a un AGENTE responder a un ticket."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    # CAMBIO: Usar 'municipio' para seleccionar el modelo
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket # <--- CAMBIO AQUÍ
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    has_permission_to_respond = False
    # CAMBIO: Usar 'municipio' en la comparación de permisos
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios': # <--- CAMBIO AQUÍ
        has_permission_to_respond = True
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        if getattr(ticket_obj, 'rubro_id', None) and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id:
            has_permission_to_respond = True

    if not has_permission_to_respond:
        return jsonify({"error": "No tienes permiso para responder este ticket."}), 403

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo, # tipo_ticket aquí ya es correcto si el parámetro 'tipo' es singular
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
            "archivo_url": getattr(ticket_obj, 'archivo_url', None)
        }
        return jsonify(ticket_data), 200

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

@ticket_bp.route('/<string:tipo>/<int:ticket_id>/estado', methods=['PUT'])
@token_requerido
def cambiar_estado_ticket(current_user: User, tipo: str, ticket_id: int):
    """Cambia el estado de un ticket (resuelto, en_proceso, cerrado, etc)."""
    data = request.get_json()
    nuevo_estado = data.get("estado")
    if not nuevo_estado:
        return jsonify({"error": "Falta el nuevo estado."}), 400

    # CAMBIO: Usar 'municipio' para seleccionar el modelo
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket # <--- CAMBIO AQUÍ
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    has_permission_to_change_state = False
    # CAMBIO: Usar 'municipio' en la comparación de permisos
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios': # <--- CAMBIO AQUÍ
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
    }
    return jsonify(ticket_data)

@ticket_bp.route('/chat/<int:ticket_id>/mensajes', methods=['GET'])
@token_requerido
def get_chat_mensajes(current_user: User, ticket_id: int):
    """
    Devuelve los mensajes del chat en vivo (solo municipio).
    """
    try:
        sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
        es_dueño_del_ticket = sala_de_chat.user_id == current_user.id
        if not (es_agente_municipal or es_dueño_del_ticket):
            return jsonify({"error": "No tienes permiso para acceder a este chat."}), 403

        ultimo_mensaje_id = request.args.get('ultimo_mensaje_id', default=0, type=int)
        mensajes_nuevos = TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket_id,
            TicketComentario.id > ultimo_mensaje_id
        ).order_by(TicketComentario.fecha.asc()).all()

        mensajes_formateados = [{
            "id": msg.id,
            "texto": msg.comentario,
            "fecha": msg.fecha.isoformat(),
            "es_admin": msg.es_admin
        } for msg in mensajes_nuevos]

        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados
        }

        return jsonify(respuesta_final)

    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500

@ticket_bp.route('/chat/<int:ticket_id>/responder_ciudadano', methods=['POST'])
@token_requerido
def responder_ciudadano_a_chat(current_user: User, ticket_id: int):
    """Permite al ciudadano responder en el chat de su ticket."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
    if not sala_de_chat:
        return jsonify({"error": "Sala de chat no encontrada."}), 404

    if sala_de_chat.user_id != current_user.id:
        return jsonify({"error": "No tienes permiso para responder en este chat."}), 403

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket="municipio",
        comentario_data={"comentario": data["comentario"], "user_id": current_user.id, "es_admin": False}
    )

    if nuevo_comentario:
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

@ticket_bp.route('/panel_por_categoria', methods=['GET'])
@token_requerido
def get_panel_por_categoria(current_user: User):
    """
    Panel de tickets por categoría (solo agentes municipales).
    """
    es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    if not es_agente_municipal:
        return jsonify({"error": "No tienes permiso para acceder a este panel."}), 403

    try:
        tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
        tickets_agrupados = defaultdict(list)

        for ticket in tickets:
            # Lógica mejorada para extraer la dirección del detalle
            direccion = "No especificada"
            if ticket.detalles:
                for line in ticket.detalles.splitlines():
                    if "Dirección del problema:" in line:
                        direccion = line.split("Dirección del problema:")[1].strip()
                        break

            ticket_data = {
                "id": ticket.id,
                "tipo": "municipio", # <--- YA HECHO ESTE CAMBIO EN LA INTERACCIÓN ANTERIOR
                "nro_ticket": ticket.nro_ticket,
                "asunto": ticket.asunto,
                "estado": ticket.estado,
                "fecha": ticket.fecha.isoformat(),
                "direccion": direccion
            }
            tickets_agrupados[ticket.categoria or "Sin Categoría"].append(ticket_data)

        return jsonify(tickets_agrupados)

    except Exception as e:
        current_app.logger.error(f"Error en get_panel_por_categoria: {e}", exc_info=True)
        return jsonify({"error": "Error interno al generar el panel de tickets."}), 500