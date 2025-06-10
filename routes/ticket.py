from flask import Blueprint, request, jsonify, current_app
from models import MunicipioTicket, PymeTicket, User, TicketComentario, db # Asegúrate de importar TicketComentario
from services.ticket_service import servicio_tickets
from .auth import token_requerido

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
    """Obtiene el detalle completo de UN ticket, incluyendo su historial de comentarios."""
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket = db.session.get(TicketModel, ticket_id)

    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404
    
    # Lógica de permisos para la PYME o Municipio
    has_permission = False
    if tipo == 'municipio' and current_user.rubro.nombre.lower().strip() == 'municipios':
        has_permission = True # Usuarios de municipio pueden ver todos los tickets de municipio
    elif tipo == 'pyme' and current_user.rubro.nombre.lower().strip() != 'municipios':
        # La PYME puede ver sus PymeTickets si están asociados a su rubro_id o user_id
        # Asumo que el ticket.rubro_id es el id del rubro del current_user logueado (la PYME)
        if ticket.rubro_id and current_user.rubro_id and ticket.rubro_id == current_user.rubro_id:
            has_permission = True
        # Si el ticket fue creado por la PYME (como cliente de otro servicio)
        elif ticket.user_id == current_user.id:
            has_permission = True
    
    if not has_permission:
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    comentarios = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin} for c in ticket.comentarios]
    
    ticket_data = {
        "id": ticket.id, 
        "tipo": tipo, 
        "nro_ticket": ticket.nro_ticket, 
        "asunto": getattr(ticket, 'asunto', ''), 
        "estado": ticket.estado,
        "fecha": ticket.fecha.isoformat(), 
        "detalles": getattr(ticket, 'detalles', getattr(ticket, 'pregunta', '')), # Usa 'detalles' o 'pregunta'
        "comentarios": sorted(comentarios, key=lambda c: c['fecha']),
        # --- AÑADIR CAMPOS ESPECÍFICOS DE PYMETICKET PARA EL DETALLE ---
        "rubro_id": getattr(ticket, 'rubro_id', None),
        "telefono": getattr(ticket, 'telefono', None),
        "email": getattr(ticket, 'email', None),
        "dni": getattr(ticket, 'dni', None),
        "estado_cliente": getattr(ticket, 'estado_cliente', None),
        "archivo_url": getattr(ticket, 'archivo_url', None) # Si hay archivos adjuntos
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
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
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
        
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
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