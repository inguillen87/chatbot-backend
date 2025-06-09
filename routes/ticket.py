# routes/ticket.py
from flask import Blueprint, request, jsonify
from flask_login import login_required, current_user
from models import MunicipioTicket, PymeTicket, TicketComentario, db

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

@ticket_bp.route('/', methods=['GET'])
@login_required
def get_tickets_de_usuario():
    """
    Endpoint universal y mejorado para obtener tickets.
    Devuelve TODOS los tickets (Pymes y Municipios) para un superadmin,
    o solo los tickets correspondientes al rubro del usuario logueado.
    """
    if not current_user:
        return jsonify({"error": "Usuario no autenticado."}), 401
    
    # Aquí podrías tener una lógica para superadmin si quisieras
    # if getattr(current_user, 'es_super_admin', False):
    #     tickets_pymes = PymeTicket.query.all()
    #     tickets_municipios = MunicipioTicket.query.all()
    # else:

    # Lógica actual: Muestra los tickets según el rubro del usuario logueado
    if not current_user.rubro:
        return jsonify([]) # Si el usuario no tiene rubro, no tiene tickets que ver

    if current_user.rubro.nombre.lower().strip() == 'municipios':
        tickets = MunicipioTicket.query.order_by(MunicipioTicket.fecha.desc()).all()
        tipo = 'municipio'
    else:
        # Asumimos que cualquier otro rubro es una Pyme y que los tickets están asociados al user_id
        tickets = PymeTicket.query.filter_by(user_id=current_user.id).order_by(PymeTicket.fecha.desc()).all()
        tipo = 'pyme'

    resultado = [{
        "id": t.id, "tipo": tipo, "nro_ticket": t.nro_ticket, "asunto": getattr(t, 'asunto', 'N/A'),
        "estado": t.estado, "fecha": t.fecha.isoformat()
    } for t in tickets]
        
    return jsonify(resultado)


@ticket_bp.route('/<string:tipo>/<int:ticket_id>', methods=['GET'])
@login_required
def get_detalle_ticket(tipo: str, ticket_id: int):
    """Obtiene el detalle completo de UN ticket, incluyendo sus comentarios."""
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket = db.session.get(TicketModel, ticket_id)

    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404
    
    # Aquí iría tu lógica de permisos
    
    comentarios = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_agente": c.es_agente} for c in ticket.comentarios]
    
    ticket_data = {
        "id": ticket.id, "nro_ticket": ticket.nro_ticket, "asunto": getattr(ticket, 'asunto', ''),
        "estado": ticket.estado, "fecha": ticket.fecha.isoformat(),
        "detalles": getattr(ticket, 'detalles', getattr(ticket, 'pregunta', '')),
        "comentarios": sorted(comentarios, key=lambda c: c['fecha'])
    }
    return jsonify(ticket_data)


@ticket_bp.route('/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@login_required
def responder_a_ticket(tipo: str, ticket_id: int):
    """Endpoint para que un AGENTE pueda responder a un ticket."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    from services.ticket_service import servicio_tickets
    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo,
        comentario_data={"comentario": data["comentario"], "es_agente": True}
    )
    
    if nuevo_comentario:
        TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
        ticket = db.session.get(TicketModel, ticket_id)
        if ticket.estado == "nuevo": ticket.estado = "en_proceso"
        db.session.commit()
        return jsonify({"mensaje": "Respuesta enviada con éxito."}), 201
    
    return jsonify({"error": "No se pudo guardar la respuesta."}), 500