from flask import Blueprint, request, jsonify, current_app
from models import MunicipioTicket, PymeTicket, User, TicketComentario, db # Asegúrate de importar TicketComentario
from services.ticket_service import servicio_tickets
from .auth import token_requerido
from collections import defaultdict # Importa defaultdict

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

@ticket_bp.route('/', methods=['GET'])
@token_requerido
def get_tickets_del_usuario(current_user: User):
    """
    Endpoint universal y mejorado para obtener la lista de tickets.
    Determina si el usuario es de una Pyme o Municipio y devuelve los tickets correspondientes.
    """
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar tickets."}), 404

    try:
        if current_user.rubro.nombre.lower().strip() == 'municipios':
            # Si es un usuario de Municipio, puede ver todos los tickets de municipio.
            tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
            tipo = 'municipio'
            # No hay campos específicos de PymeTicket aquí
            def serialize_ticket(t):
                return {
                    "id": t.id,
                    "tipo": tipo,
                    "nro_ticket": t.nro_ticket,
                    "asunto": getattr(t, 'asunto', 'N/A'),
                    "estado": t.estado,
                    "fecha": t.fecha.isoformat(),
                    # Campos específicos de MunicipioTicket si los necesitas en la lista
                    # "pregunta": t.pregunta, 
                    # "categoria": t.categoria
                }
        else: # Asumimos que es una PYME
            # Si es una Pyme, ve sus propios tickets (los creados por ella)
            # Y también tickets que fueron creados por clientes *para* su rubro
            # Esto depende de cómo quieras vincular PymeTickets a la PYME.
            # Opción 1: Tickets creados por este user (si la PYME es también cliente)
            # tickets = PymeTicket.query.filter_by(user_id=current_user.id).order_by(PymeTicket.fecha.desc()).all()

            # Opción 2 (más probable para gestión): Tickets asociados a su rubro.
            # Esto requiere que PymeTicket tenga un rubro_id o rubro_nombre asociado a la PYME.
            # Tu PymeTicket ya tiene 'rubro_id'.
            if current_user.rubro_id:
                tickets = PymeTicket.query.filter_by(rubro_id=current_user.rubro_id).order_by(PymeTicket.fecha.desc()).all()
            else: # Fallback si la PYME no tiene rubro_id
                tickets = PymeTicket.query.filter_by(user_id=current_user.id).order_by(PymeTicket.fecha.desc()).all()

            tipo = 'pyme'
            # Campos específicos de PymeTicket para la serialización
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
                    # Otros campos de PymeTicket si necesitas en la lista
                }
        
        resultado = [serialize_ticket(t) for t in tickets]
        return jsonify(resultado)
    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500


@ticket_bp.route('/<string:tipo>/<int:ticket_id>', methods=['GET'])
@token_requerido
def get_detalle_ticket(current_user: User, tipo: str, ticket_id: int):
    """
    Devuelve el detalle completo de UN ticket: datos, comentarios, contacto, etc.
    """
    TicketModel = MunicipioTicket if tipo == "municipios" else PymeTicket
    ticket = db.session.get(TicketModel, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # Permisos
    is_admin_muni = tipo == 'municipios' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    is_dueño = ticket.user_id == current_user.id
    is_admin_pyme = tipo == 'pyme' and current_user.rubro_id and ticket.rubro_id == current_user.rubro_id
    if not (is_admin_muni or is_dueño or is_admin_pyme):
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    # --- Extraer datos contacto ---
    detalles = ticket.detalles or ""
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
        "detalles": getattr(ticket, 'detalles', getattr(ticket, 'pregunta', '')),
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
    """Endpoint para que un AGENTE pueda responder a un ticket."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400
    
    # Lógica de permisos de agente (solo el dueño de la PYME/Municipio puede responder)
    TicketModel = MunicipioTicket if tipo == "municipios" else PymeTicket # <-- CORREGIDO
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # Verificar si el usuario logueado es el agente/dueño correcto para este tipo de ticket
    has_permission_to_respond = False
    if tipo == 'municipio' and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_permission_to_respond = True
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        # La PYME responde a tickets de su rubro
        if ticket_obj.rubro_id and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id:
            has_permission_to_respond = True
    
    if not has_permission_to_respond:
        return jsonify({"error": "No tienes permiso para responder este ticket."}), 403

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo,
        comentario_data={"comentario": data["comentario"], "user_id": current_user.id, "es_admin": True}
    )
    
    if nuevo_comentario:
        # Actualizar el estado del ticket a 'en_proceso' si estaba en 'nuevo'
        if ticket_obj.estado == "nuevo":
            ticket_obj.estado = "en_proceso"
            db.session.commit()
        
        # Devolver el ticket actualizado completo para que el frontend no tenga que hacer otra petición
        # Reutilizamos la lógica de get_detalle_ticket
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
        return jsonify(ticket_data), 200 # Devolver 200 OK y el objeto actualizado
    
    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

@ticket_bp.route('/<string:tipo>/<int:ticket_id>/estado', methods=['PUT'])
@token_requerido
def cambiar_estado_ticket(current_user: User, tipo: str, ticket_id: int):
    """Endpoint para cambiar el estado de un ticket."""
    data = request.get_json()
    nuevo_estado = data.get("estado")
    if not nuevo_estado:
        return jsonify({"error": "Falta el nuevo estado."}), 400
        
    TicketModel = MunicipioTicket if tipo == "municipios" else PymeTicket # <-- CORREGIDO
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404
        
    # Lógica de permisos para cambiar el estado (similar a responder)
    has_permission_to_change_state = False
    if tipo == 'municipio' and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_permission_to_change_state = True
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        if ticket_obj.rubro_id and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id:
            has_permission_to_change_state = True
    
    if not has_permission_to_change_state:
        return jsonify({"error": "No tienes permiso para cambiar el estado de este ticket."}), 403

    ticket_obj.estado = nuevo_estado
    db.session.commit()
    
    # Devolver el ticket actualizado completo
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
    Endpoint para el polling del chat en vivo del municipio.
    Devuelve los nuevos mensajes y el estado actual de una sala de chat (ticket).
    """
    try:
        # 1. Buscar la sala de chat, que es un MunicipioTicket
        sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        # 2. LÓGICA DE PERMISOS CORRECTA Y FINAL
        # Se verifica si el usuario es un agente municipal basándose en su rubro.
        es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
        
        # Se verifica si el usuario es el creador del ticket.
        es_dueño_del_ticket = sala_de_chat.user_id == current_user.id

        # Si no es ni agente ni dueño, no tiene acceso.
        if not (es_agente_municipal or es_dueño_del_ticket):
            return jsonify({"error": "No tienes permiso para acceder a este chat."}), 403

        # 3. Obtener el último mensaje que el cliente ya tiene
        ultimo_mensaje_id = request.args.get('ultimo_mensaje_id', default=0, type=int)

        # 4. Consultar solo los mensajes nuevos
        mensajes_nuevos = TicketComentario.query.filter(
            TicketComentario.municipio_ticket_id == ticket_id,
            TicketComentario.id > ultimo_mensaje_id
        ).order_by(TicketComentario.fecha.asc()).all()

        # 5. Formatear la Respuesta JSON (esto ya estaba bien)
        mensajes_formateados = [{
            "id": msg.id,
            "texto": msg.comentario,
            "fecha": msg.fecha.isoformat(),
            "es_admin": msg.es_admin  # Usamos el campo del modelo TicketComentario
        } for msg in mensajes_nuevos]

        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados
        }

        return jsonify(respuesta_final)

    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500
    
    # En ticket_bp.py

@ticket_bp.route('/chat/<int:ticket_id>/responder_ciudadano', methods=['POST'])
@token_requerido
def responder_ciudadano_a_chat(current_user: User, ticket_id: int):
    """Endpoint para que un CIUDADANO envíe un mensaje a un chat en vivo existente."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400
    
    sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
    if not sala_de_chat:
        return jsonify({"error": "Sala de chat no encontrada."}), 404

    # Verificamos que solo el dueño del ticket pueda escribir en él.
    if sala_de_chat.user_id != current_user.id:
        return jsonify({"error": "No tienes permiso para responder en este chat."}), 403

    # Usamos tu servicio para crear el comentario, marcando es_admin como False
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
    Endpoint para el panel de administración. Devuelve todos los tickets de municipio,
    agrupados en un diccionario por categoría.
    VERSIÓN FINAL Y CORREGIDA.
    """
    # --- Permisos: Solo los agentes del municipio pueden ver este panel ---
    es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    if not es_agente_municipal:
        return jsonify({"error": "No tienes permiso para acceder a este panel."}), 403

    try:
        # 1. Obtenemos TODOS los tickets de municipio, ordenados por fecha reciente
        tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()

        # 2. Creamos un diccionario para agrupar los tickets
        tickets_agrupados = defaultdict(list)

        # 3. Iteramos y agrupamos cada ticket en su categoría
        for ticket in tickets:
            # Serializamos la información esencial del ticket
            ticket_data = {
                "id": ticket.id,
                "tipo": "municipios", # <--- LA LÍNEA MÁGICA
                "nro_ticket": ticket.nro_ticket,
                "asunto": ticket.asunto,
                "estado": ticket.estado,
                "fecha": ticket.fecha.isoformat(),
                "direccion": (
                    ticket.detalles.split("Dirección del problema:")[1].split("\n")[0].strip()
                    if ticket.detalles and "Dirección del problema:" in ticket.detalles
                    else "No especificada"
                )
            }
            tickets_agrupados[ticket.categoria or "Sin Categoría"].append(ticket_data)

        return jsonify(tickets_agrupados)

    except Exception as e:
        current_app.logger.error(f"Error en get_panel_por_categoria: {e}", exc_info=True)
        return jsonify({"error": "Error interno al generar el panel de tickets."}), 500