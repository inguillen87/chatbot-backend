# Contenido para: routes/ticket.py

from flask import Blueprint, request, jsonify, current_app
# 1. Importa nuestro decorador de autenticación unificado
from routes.auth import token_requerido 
from models import MunicipioTicket
from extensions import db

# 2. Aquí se define el 'ticket_bp' que app.py necesita importar
ticket_bp = Blueprint('ticket_bp', __name__)

@ticket_bp.route('/tickets/municipio', methods=['GET'])
@token_requerido # 3. Se usa el decorador correcto
def get_tickets_municipio(current_user): # 4. La función recibe 'current_user'
    """
    Obtiene los tickets para un usuario de tipo Municipio.
    El usuario 'current_user' es inyectado por el decorador @token_requerido.
    """
    user_id_param = request.args.get('user_id', type=int)
    
    # Verificación de seguridad (opcional pero recomendada)
    if not hasattr(current_user, 'es_admin') or (not current_user.es_admin and current_user.id != user_id_param):
        return jsonify({"error": "No tienes permiso para ver estos tickets."}), 403

    try:
        tickets = MunicipioTicket.query.filter_by(user_id=user_id_param).order_by(MunicipioTicket.fecha.desc()).all()
        
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
        current_app.logger.error(f"Error en get_tickets_municipio para user_id {user_id_param}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

# Agrega aquí tus otras rutas de tickets (ej. para Pymes) siguiendo el mismo patrón.