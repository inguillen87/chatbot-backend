# routes/ticket.py

from flask import Blueprint, request, jsonify, current_app
from flask_login import current_user, login_required # Usamos login_required para proteger estas rutas
from models import MunicipioTicket, PymeTicket, User, db
from services.ticket_service import servicio_tickets

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

# --- Helper para no repetir código ---
def get_ticket_model_and_check_permission(ticket_id):
    # Primero, intentamos encontrar el ticket en PymeTicket
    ticket = db.session.get(PymeTicket, ticket_id)
    tipo = "pyme"
    if not ticket:
        # Si no está en Pyme, lo buscamos en MunicipioTicket
        ticket = db.session.get(MunicipioTicket, ticket_id)
        tipo = "municipio"
    
    if not ticket:
        return None, None, jsonify({"error": "Ticket no encontrado."}), 404
        
    # Verificación de seguridad: un admin puede ver todo, un usuario solo lo suyo
    # Asumimos que los tickets de municipio son públicos para los agentes logueados
    if tipo == "pyme" and not getattr(current_user, 'es_admin', False) and current_user.id != ticket.user_id:
        return None, None, jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    return ticket, tipo, None, None


# --- RUTAS DE LA API ---

@ticket_bp.route('/', methods=['GET'])
@login_required
def get_tickets_del_usuario():
    """
    Endpoint para obtener la lista de tickets de un usuario (para el panel).
    Determina si son de Pyme o Municipio basado en el rubro del usuario.
    """
    # ... (Tu lógica para listar tickets se mantiene, es buena, la integramos aquí)
    usuario = current_user
    if not usuario or not usuario.rubro:
        return jsonify({"error": "Usuario o rubro no encontrado."}), 404
    
    TicketModel = MunicipioTicket if usuario.rubro.nombre.lower().strip() == 'municipios' else PymeTicket
    tickets = TicketModel.query.filter_by(user_id=usuario.id).order_by(TicketModel.fecha.desc()).all()
    
    resultado = [{
        "id": t.id, "nro_ticket": t.nro_ticket, "asunto": getattr(t, 'asunto', 'N/A'),
        "categoria": getattr(t, 'categoria', 'N/A'), "estado": t.estado, "fecha": t.fecha.isoformat()
    } for t in tickets]
        
    return jsonify(resultado)

@ticket_bp.route('/<int:ticket_id>', methods=['GET'])
@login_required
def get_detalle_ticket(ticket_id):
    """
    NUEVO: Endpoint para obtener el detalle completo de UN ticket,
    incluyendo su historial de comentarios.
    """
    ticket, tipo, error_response, status_code = get_ticket_model_and_check_permission(ticket_id)
    if error_response:
        return error_response, status_code

    comentarios = [{"comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_agente": c.es_agente} for c in ticket.comentarios]
    
    ticket_data = {
        "id": ticket.id, "nro_ticket": ticket.nro_ticket, "asunto": getattr(ticket, 'asunto', ''),
        "categoria": getattr(ticket, 'categoria', ''), "estado": ticket.estado,
        "fecha": ticket.fecha.isoformat(), "detalles": getattr(ticket, 'detalles', getattr(ticket, 'pregunta', '')),
        "comentarios": comentarios
    }
    return jsonify(ticket_data)


@ticket_bp.route('/<int:ticket_id>/responder', methods=['POST'])
@login_required
def responder_a_ticket(ticket_id):
    """
    NUEVO: Endpoint para que un AGENTE pueda responder a un ticket.
    Esto cumple tu requisito de "poder responder".
    """
    ticket, tipo, error_response, status_code = get_ticket_model_and_check_permission(ticket_id)
    if error_response:
        return error_response, status_code
    
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400
    
    # Usamos nuestro servicio de tickets para crear el comentario
    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket.id,
        tipo_ticket=tipo,
        comentario_data={
            "comentario": data["comentario"],
            "user_id": current_user.id, # El ID del agente que responde
            "es_agente": True  # Marcamos que la respuesta viene del panel
        }
    )
    
    if nuevo_comentario:
        # Al responder, cambiamos el estado del ticket a "en proceso"
        ticket.estado = "en_proceso"
        db.session.commit()
        return jsonify({"mensaje": "Respuesta enviada con éxito.", "comentario_id": nuevo_comentario.id}), 201
    
    return jsonify({"error": "No se pudo guardar la respuesta."}), 500


@ticket_bp.route('/<int:ticket_id>/estado', methods=['PUT'])
@login_required
def cambiar_estado_ticket(ticket_id):
    """
    NUEVO: Endpoint para cambiar el estado de un ticket.
    Cumple tu requisito de "resuelto, derivado, etc.".
    """
    ticket, _, error_response, status_code = get_ticket_model_and_check_permission(ticket_id)
    if error_response:
        return error_response, status_code

    data = request.get_json()
    nuevo_estado = data.get("estado")
    if not nuevo_estado:
        return jsonify({"error": "Falta el nuevo estado."}), 400

    # Aquí podrías validar que el estado sea uno de los permitidos
    # estados_validos = ["nuevo", "en_proceso", "resuelto", "derivado"] ...
    
    ticket.estado = nuevo_estado
    db.session.commit()
    
    return jsonify({"mensaje": f"El estado del ticket #{ticket.nro_ticket} ha sido actualizado a '{nuevo_estado}'."})