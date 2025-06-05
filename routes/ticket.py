from flask import Blueprint, request, jsonify
from models import MunicipioTicket, PymeTicket, TicketComentario, db
from services.ticket import crear_comentario_ticket

ticket_bp = Blueprint("ticket_bp", __name__)

# Crear ticket (opcional manual)
@ticket_bp.route('/tickets/<tipo_ticket>', methods=['POST'])
def crear_ticket(tipo_ticket):
    data = request.get_json() or {}
    pregunta = data.get("pregunta", "").strip()
    user_id = data.get("user_id")
    estado = data.get("estado", "nuevo")
    archivo_url = data.get("archivo_url")
    telefono = data.get("telefono")
    email = data.get("email")
    dni = data.get("dni")
    estado_cliente = data.get("estado_cliente", "no_definido")

    if tipo_ticket not in ("municipio", "pyme"):
        return jsonify({"ok": False, "error": "Tipo de ticket inválido"}), 400
    if not pregunta:
        return jsonify({"ok": False, "error": "La pregunta/reclamo es obligatoria"}), 400

    import random
    nro_ticket = random.randint(10000, 99999)

    if tipo_ticket == "municipio":
        ticket = MunicipioTicket(
            pregunta=pregunta,
            user_id=user_id,
            estado=estado,
            nro_ticket=nro_ticket,
            archivo_url=archivo_url
        )
    else:
        ticket = PymeTicket(
            pregunta=pregunta,
            user_id=user_id,
            estado=estado,
            nro_ticket=nro_ticket,
            archivo_url=archivo_url,
            telefono=telefono,
            email=email,
            dni=dni,
            estado_cliente=estado_cliente
        )
    db.session.add(ticket)
    db.session.commit()
    return jsonify({"ok": True, "ticket": ticket.id, "nro_ticket": nro_ticket}), 201

# Listar tickets del admin (filtra por user_id)
@ticket_bp.route('/tickets/<tipo_ticket>', methods=['GET'])
def listar_tickets(tipo_ticket):
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"ok": False, "error": "Falta user_id para filtrar tickets"}), 400

    query = MunicipioTicket.query if tipo_ticket == "municipio" else PymeTicket.query
    tickets = query.filter_by(user_id=user_id).order_by(query.column_descriptions[0]['entity'].fecha.desc()).all()

    data = []
    for t in tickets:
        data.append({
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "pregunta": t.pregunta,
            "estado": t.estado,
            "user_id": t.user_id,
            "fecha": t.fecha.isoformat() if t.fecha else None,
            "telefono": getattr(t, "telefono", None),
            "email": getattr(t, "email", None),
            "dni": getattr(t, "dni", None),
            "estado_cliente": getattr(t, "estado_cliente", None),
        })
    return jsonify({"ok": True, "tickets": data})

# Cambiar estado (solo admin del ticket)
@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>/estado', methods=['PUT'])
def cambiar_estado_ticket(tipo_ticket, ticket_id):
    data = request.get_json() or {}
    nuevo_estado = data.get("estado")
    user_id = data.get("user_id")
    if not nuevo_estado or not user_id:
        return jsonify({"ok": False, "error": "Estado y user_id son obligatorios"}), 400

    ticket = MunicipioTicket.query.get(ticket_id) if tipo_ticket == "municipio" else PymeTicket.query.get(ticket_id)
    if not ticket:
        return jsonify({"ok": False, "error": "Ticket no encontrado"}), 404
    if ticket.user_id != user_id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    ticket.estado = nuevo_estado
    db.session.commit()
    return jsonify({"ok": True, "ticket": ticket.id, "nuevo_estado": ticket.estado})

# Detalle ticket (solo admin)
@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>', methods=['GET'])
def detalle_ticket(tipo_ticket, ticket_id):
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"ok": False, "error": "Falta user_id"}), 400

    ticket = MunicipioTicket.query.get(ticket_id) if tipo_ticket == "municipio" else PymeTicket.query.get(ticket_id)
    if not ticket:
        return jsonify({"ok": False, "error": "Ticket no encontrado"}), 404
    if ticket.user_id != user_id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    data = {
        "id": ticket.id,
        "nro_ticket": ticket.nro_ticket,
        "pregunta": ticket.pregunta,
        "estado": ticket.estado,
        "user_id": ticket.user_id,
        "fecha": ticket.fecha.isoformat() if ticket.fecha else None,
        "telefono": getattr(ticket, "telefono", None),
        "email": getattr(ticket, "email", None),
        "dni": getattr(ticket, "dni", None),
        "estado_cliente": getattr(ticket, "estado_cliente", None),
    }
    return jsonify({"ok": True, "ticket": data})

# Agregar comentario (admin o vecino)
@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>/comentarios', methods=['POST'])
def agregar_comentario(tipo_ticket, ticket_id):
    data = request.get_json() or {}
    comentario = data.get("comentario", "").strip()
    user_id = data.get("user_id")
    telefono = data.get("telefono")
    email = data.get("email")
    dni = data.get("dni")
    estado_cliente = data.get("estado_cliente", "no_definido")

    if tipo_ticket not in ("municipio", "pyme"):
        return jsonify({"ok": False, "error": "Tipo de ticket inválido"}), 400
    ticket = MunicipioTicket.query.get(ticket_id) if tipo_ticket == "municipio" else PymeTicket.query.get(ticket_id)
    if not ticket:
        return jsonify({"ok": False, "error": "Ticket no encontrado"}), 404
    if not comentario:
        return jsonify({"ok": False, "error": "Comentario vacío"}), 400
    if ticket.user_id != user_id and user_id is not None:
        # Vecino puede comentar si no es admin, si es admin debe ser dueño del ticket
        pass # aquí puedes agregar lógica para validar vecinos si lo deseas

    nuevo_com = crear_comentario_ticket(
        ticket_id, tipo_ticket, comentario, user_id, telefono, email, dni, estado_cliente
    )
    db.session.commit()
    return jsonify({"ok": True, "comentario": nuevo_com.id}), 201

# Listar comentarios (solo admin o vecino)
@ticket_bp.route('/tickets/<tipo_ticket>/<int:ticket_id>/comentarios', methods=['GET'])
def listar_comentarios(tipo_ticket, ticket_id):
    user_id = request.args.get("user_id", type=int)
    if tipo_ticket not in ("municipio", "pyme"):
        return jsonify({"ok": False, "error": "Tipo de ticket inválido"}), 400
    ticket = MunicipioTicket.query.get(ticket_id) if tipo_ticket == "municipio" else PymeTicket.query.get(ticket_id)
    if not ticket:
        return jsonify({"ok": False, "error": "Ticket no encontrado"}), 404
    if ticket.user_id != user_id:
        return jsonify({"ok": False, "error": "No autorizado"}), 403

    comentarios = TicketComentario.query.filter_by(ticket_id=ticket_id, tipo_ticket=tipo_ticket).order_by(TicketComentario.fecha.asc()).all()
    data = [
        {
            "id": c.id,
            "comentario": getattr(c, "comentario", getattr(c, "mensaje", "")),
            "fecha": c.fecha.isoformat() if c.fecha else None,
            "user_id": c.user_id,
            "telefono": c.telefono,
            "email": c.email,
            "dni": c.dni,
            "estado_cliente": c.estado_cliente,
            "es_admin": getattr(c, "es_admin", False),
        }
        for c in comentarios
    ]
    return jsonify({"ok": True, "comentarios": data})
