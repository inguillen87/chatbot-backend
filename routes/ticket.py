from flask import Blueprint, request, jsonify, current_app
from models import MunicipioTicket, PymeTicket, User, db
from services.ticket_service import servicio_tickets
from .auth import token_requerido # <-- USAMOS NUESTRO DECORADOR

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
        else:
            # Si es una Pyme, solo ve sus propios tickets.
            tickets = PymeTicket.query.filter_by(user_id=current_user.id).order_by(PymeTicket.fecha.desc()).all()
            tipo = 'pyme'
        
        resultado = [{
            "id": t.id,
            "tipo": tipo, # Devolvemos el tipo para que el frontend construya la URL correcta
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, 'asunto', 'N/A'),
            "estado": t.estado,
            "fecha": t.fecha.isoformat()
        } for t in tickets]
            
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
    
    # Aquí va tu lógica de permisos, por ejemplo:
    if tipo == 'pyme' and ticket.user_id != current_user.id:
         return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    comentarios = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_agente": c.es_agente} for c in ticket.comentarios]
    
    ticket_data = {
        "id": ticket.id, "tipo": tipo, "nro_ticket": ticket.nro_ticket, 
        "asunto": getattr(ticket, 'asunto', ''), "estado": ticket.estado,
        "fecha": ticket.fecha.isoformat(), 
        "detalles": getattr(ticket, 'detalles', getattr(ticket, 'pregunta', '')),
        "comentarios": sorted(comentarios, key=lambda c: c['fecha'])
    }
    return jsonify(ticket_data)


@ticket_bp.route('/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@token_requerido
def responder_a_ticket(current_user: User, tipo: str, ticket_id: int):
    """Endpoint para que un AGENTE pueda responder a un ticket."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400
    
    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo,
        comentario_data={"comentario": data["comentario"], "user_id": current_user.id, "es_agente": True}
    )
    
    if nuevo_comentario:
        ticket = db.session.get(MunicipioTicket if tipo == "municipio" else PymeTicket, ticket_id)
        if ticket and ticket.estado == "nuevo":
            ticket.estado = "en_proceso"
            db.session.commit()
        return jsonify({"mensaje": "Respuesta enviada con éxito."}), 201
    
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
    ticket = db.session.get(TicketModel, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404
        
    ticket.estado = nuevo_estado
    db.session.commit()
    
    return jsonify({"mensaje": f"El estado del ticket #{ticket.nro_ticket} ha sido actualizado."})