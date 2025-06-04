from flask import Blueprint, request, jsonify
from models import MunicipioTicket, PymeTicket, TicketComentario
from services.ticket import crear_comentario_ticket

ticket_bp = Blueprint("ticket_bp", __name__)

@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>/comentarios', methods=['POST'])
def agregar_comentario(tipo_ticket, ticket_id):
    try:
        data = request.get_json() or {}
        comentario = data.get("comentario", "")
        user_id = data.get("user_id")
        telefono = data.get("telefono")
        email = data.get("email")
        dni = data.get("dni")
        estado_cliente = data.get("estado_cliente", "no_definido")

        if tipo_ticket not in ("municipio", "pyme"):
            return jsonify({"error": "Tipo de ticket inválido"}), 400

        if not comentario.strip():
            return jsonify({"error": "Comentario vacío"}), 400

        nuevo_com = crear_comentario_ticket(
            ticket_id, tipo_ticket, comentario, user_id, telefono, email, dni, estado_cliente
        )
        return jsonify({"ok": True, "comentario": nuevo_com.id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>/comentarios', methods=['GET'])
def listar_comentarios(tipo_ticket, ticket_id):
    if tipo_ticket == "municipio":
        ticket = MunicipioTicket.query.get(ticket_id)
    elif tipo_ticket == "pyme":
        ticket = PymeTicket.query.get(ticket_id)
    else:
        return jsonify({"error": "Tipo de ticket inválido"}), 400

    if not ticket:
        return jsonify({"error": "Ticket no encontrado"}), 404

    # .all() ya funciona si comentarios es una relación, pero asegúrate en el modelo que sea lazy="dynamic"
    comentarios = ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
    data = [
        {
            "id": c.id,
            "comentario": c.comentario,
            "fecha": c.fecha.isoformat() if c.fecha else None,
            "user_id": c.user_id,
            "telefono": c.telefono,
            "email": c.email,
            "dni": c.dni,
            "estado_cliente": c.estado_cliente,
        }
        for c in comentarios
    ]
    return jsonify({"comentarios": data})
