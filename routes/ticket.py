# Contenido COMPLETO y FINAL para: routes/ticket.py

from flask import Blueprint, request, jsonify, current_app
from routes.auth import token_requerido 
from models import MunicipioTicket, PymeTicket, User # Importamos los dos modelos de Ticket
from extensions import db


ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

@ticket_bp.route('/', methods=['GET'])
@token_requerido
def get_tickets_del_usuario(current_user):
    """
    Endpoint universal para obtener tickets.
    Detecta automáticamente el rubro del usuario y devuelve los tickets correspondientes.
    """
    user_id_param = request.args.get('user_id', type=int)

    # Verificación de seguridad: solo un admin o el propio usuario pueden ver sus tickets
    if not getattr(current_user, 'es_admin', False) and current_user.id != user_id_param:
        return jsonify({"error": "No tienes permiso para ver estos tickets."}), 403

    # Buscamos al usuario para saber su rubro
    usuario_a_consultar = db.session.get(User, user_id_param)
    if not usuario_a_consultar or not usuario_a_consultar.rubro:
        return jsonify({"error": "Usuario o rubro no encontrado."}), 404

    try:
        # --- LÓGICA INTELIGENTE ---
        # Decidimos qué tabla consultar basado en el nombre del rubro
        if usuario_a_consultar.rubro.nombre.lower().strip() == 'municipios':
            current_app.logger.info(f"Buscando tickets de tipo Municipio para user_id {user_id_param}")
            TicketModel = MunicipioTicket
        else:
            current_app.logger.info(f"Buscando tickets de tipo PyME para user_id {user_id_param}")
            TicketModel = PymeTicket
            
        tickets = TicketModel.query.filter_by(user_id=user_id_param).order_by(TicketModel.fecha.desc()).all()
        
        resultado = []
        for ticket in tickets:
            resultado.append({
                "id": ticket.id,
                "pregunta": ticket.pregunta,
                "estado": ticket.estado,
                "nro_ticket": ticket.nro_ticket,
                "fecha": ticket.fecha.isoformat(),
                "comentarios_count": ticket.comentarios.count()
            })
        
        return jsonify(resultado)

    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user_id {user_id_param}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

# Agrega aquí tus otras rutas, como la de crear comentarios, si es necesario.