from flask import Blueprint, request, jsonify, current_app
from models import (
    MunicipioTicket,
    PymeTicket,
    User,
    TicketComentario,
    TicketSatisfaccion,
    db,
)
from services.ticket_service import servicio_tickets
from .auth import token_requerido, anon_o_token_requerido, admin_o_empleado_requerido
from collections import defaultdict

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

MENSAJE_CHAT_CERRADO = "El chat fue cerrado"
MENSAJE_SIN_PERMISOS = "No tienes permiso para acceder a este chat."

def log_ticket_debug(action: str, ticket_id: int, header_anon_id: str | None, ticket_obj) -> None:
    """Registro unificado de acciones sobre tickets."""
    current_app.logger.info(
        "%s | ticket=%s anon_id=%s anon_db=%s estado=%s",
        action,
        ticket_id,
        header_anon_id,
        getattr(ticket_obj, "anon_id", None),
        getattr(ticket_obj, "estado", None),
    )

# ---------- LISTA DE TICKETS (logueado) ----------
@ticket_bp.route('/', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_tickets_del_usuario(current_user: User):
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar tickets."}), 404

    try:
        if current_user.rubro.nombre.lower().strip() == 'municipios':
            # Tickets municipales (no filtrados por municipio_id)
            tickets = (
                MunicipioTicket.query
                .order_by(MunicipioTicket.fecha.desc())
                .all()
            )
            tipo = 'municipio'
            def serialize_ticket(t):
                return {
                    "id": t.id,
                    "tipo": tipo,
                    "nro_ticket": t.nro_ticket,
                    "asunto": getattr(t, 'asunto', 'N/A'),
                    "estado": t.estado,
                    "fecha": t.fecha.isoformat(),
                    "categoria": getattr(t, 'categoria', None),
                    "direccion": getattr(t, 'direccion', None),
                    "latitud": getattr(t, 'latitud', None),
                    "longitud": getattr(t, 'longitud', None)
                }
        else:
            # Solo tickets de su empresa/rubro
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
                    "categoria": getattr(t, 'categoria', None),
                    "direccion": getattr(t, 'direccion', None),
                    "latitud": getattr(t, 'latitud', None),
                    "longitud": getattr(t, 'longitud', None)
                }
        resultado = [serialize_ticket(t) for t in tickets]
        return jsonify(resultado)
    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

# ---------- LISTA DE MIS TICKETS (cliente) ----------
@ticket_bp.route('/mios', methods=['GET'])
@token_requerido
def get_mis_tickets(current_user: User):
    """Devuelve solo los tickets asociados al usuario autenticado."""
    try:
        tickets_muni = (
            MunicipioTicket.query
            .filter_by(user_id=current_user.id)
            .order_by(MunicipioTicket.fecha.desc())
            .all()
        )
        tickets_pyme = (
            PymeTicket.query
            .filter_by(user_id=current_user.id)
            .order_by(PymeTicket.fecha.desc())
            .all()
        )

        def serialize(t, tipo):
            base = {
                "id": t.id,
                "tipo": tipo,
                "nro_ticket": t.nro_ticket,
                "asunto": getattr(t, "asunto", "N/A"),
                "estado": t.estado,
                "fecha": t.fecha.isoformat(),
                "direccion": getattr(t, "direccion", None),
                "latitud": getattr(t, "latitud", None),
                "longitud": getattr(t, "longitud", None),
            }
            if tipo == "pyme":
                base.update({
                    "telefono": getattr(t, "telefono", None),
                    "email": getattr(t, "email", None),
                    "dni": getattr(t, "dni", None),
                    "estado_cliente": getattr(t, "estado_cliente", None),
                })
            else:
                base.update({
                    "categoria": getattr(t, "categoria", None),
                })
            return base

        todos = [serialize(t, "municipio") for t in tickets_muni] + [
            serialize(t, "pyme") for t in tickets_pyme
        ]
        todos.sort(key=lambda x: x["fecha"], reverse=True)
        return jsonify(todos)
    except Exception as e:
        current_app.logger.error(
            f"Error en get_mis_tickets para user {getattr(current_user,'id','?')}: {e}",
            exc_info=True,
        )
        return jsonify({"error": "Error interno al obtener tus tickets."}), 500

# ---------- DETALLE DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>', methods=['GET'])
@anon_o_token_requerido
def detalle_ticket(current_user, tipo, ticket_id):
    """
    Devuelve el detalle de un ticket, reforzando la lógica de permisos para admins, empleados y usuarios.
    """
    anon_id = request.headers.get("Anon-Id")
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket = db.session.get(TicketModel, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # --- PERMISOS ---
    is_dueño = current_user and ticket.user_id == current_user.id
    is_admin_muni = (
        current_user
        and tipo == "municipio"
        and getattr(current_user, "rubro", None)
        and current_user.rubro.nombre.lower().strip() == "municipios"
        and hasattr(current_user, "municipio_id")
        and getattr(ticket, "municipio_id", None) == current_user.municipio_id
    )
    is_admin_pyme = (
        current_user
        and tipo == "pyme"
        and getattr(current_user, "rubro_id", None)
        and getattr(ticket, "rubro_id", None) == current_user.rubro_id
    )
    is_anon = anon_id and getattr(ticket, "anon_id", None) == anon_id

    if not (is_dueño or is_admin_muni or is_admin_pyme or is_anon):
        current_app.logger.warning(
            f"PERMISO DENEGADO | endpoint={request.endpoint} | ticket_id={ticket_id} | anon_id_recibido={anon_id} | anon_id_ticket={getattr(ticket,'anon_id', None)} | user_id={getattr(current_user,'id', None)} | ticket_user_id={getattr(ticket,'user_id', None)} | estado={getattr(ticket,'estado', None)}"
        )
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    if ticket.estado == "cerrado" and not (is_admin_muni or is_admin_pyme):
        return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    # --- SERIALIZACIÓN ---
    detalles = getattr(ticket, 'detalles', '') or ''
    nombre, tel, email = "No especificado", "No especificado", "No especificado"
    direccion = getattr(ticket, 'direccion', None) or "No especificada"
    if "Nombre:" in detalles: nombre = detalles.split("Nombre:")[1].split("\n")[0].strip()
    if "Teléfono:" in detalles: tel = detalles.split("Teléfono:")[1].split("\n")[0].strip()
    if "Email:" in detalles: email = detalles.split("Email:")[1].split("\n")[0].strip()
    if not getattr(ticket, 'direccion', None) and "Dirección:" in detalles:
        direccion = detalles.split("Dirección:")[1].split("\n")[0].strip()

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
        "archivo_url": getattr(ticket, 'archivo_url', None),
        "latitud": getattr(ticket, 'latitud', None),
        "longitud": getattr(ticket, 'longitud', None)
    }
    return jsonify(ticket_data)

# ---------- RESPONDER A TICKET (AGENTE) ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def responder_a_ticket(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    if ticket_obj.estado == "cerrado":
        return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    # Refuerzo de permisos:
    if tipo == 'municipio':
        if not (
            current_user.rubro and
            current_user.rubro.nombre.lower().strip() == 'municipios' and
            hasattr(current_user, "municipio_id") and
            ticket_obj.municipio_id == current_user.municipio_id
        ):
            return jsonify({"error": "No tienes permiso para responder este ticket."}), 403
    elif tipo == 'pyme':
        if not (
            current_user.rubro_id and
            ticket_obj.rubro_id == current_user.rubro_id
        ):
            return jsonify({"error": "No tienes permiso para responder este ticket."}), 403

    log_ticket_debug(
        "responder_agente",
        ticket_id,
        request.headers.get("Anon-Id"),
        ticket_obj,
    )

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id, tipo_ticket=tipo,
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
            "archivo_url": getattr(ticket_obj, 'archivo_url', None),
            "latitud": getattr(ticket_obj, 'latitud', None),
            "longitud": getattr(ticket_obj, 'longitud', None)
        }
        return jsonify(ticket_data), 200

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- CAMBIAR ESTADO DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/estado', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def cambiar_estado_ticket(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json()
    nuevo_estado = data.get("estado")
    if not nuevo_estado:
        return jsonify({"error": "Falta el nuevo estado."}), 400

    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # Refuerzo de permisos:
    if tipo == 'municipio':
        if not (
            current_user.rubro and
            current_user.rubro.nombre.lower().strip() == 'municipios' and
            hasattr(current_user, "municipio_id") and
            ticket_obj.municipio_id == current_user.municipio_id
        ):
            return jsonify({"error": "No tienes permiso para cambiar el estado de este ticket."}), 403
    elif tipo == 'pyme':
        if not (
            current_user.rubro_id and
            ticket_obj.rubro_id == current_user.rubro_id
        ):
            return jsonify({"error": "No tienes permiso para cambiar el estado de este ticket."}), 403

    log_ticket_debug(
        "cambiar_estado",
        ticket_id,
        request.headers.get("Anon-Id"),
        ticket_obj,
    )

    ticket_obj.estado = nuevo_estado
    db.session.commit()
    try:
        from services.email_service import (
            enviar_email_ticket_novedad,
            enviar_sms_ticket_novedad,
        )
        enviar_email_ticket_novedad(
            ticket_obj,
            f"El estado de tu ticket ahora es '{nuevo_estado}'.",
        )
        enviar_sms_ticket_novedad(
            ticket_obj,
            f"Tu ticket {ticket_obj.nro_ticket} ahora está en '{nuevo_estado}'",
        )
    except Exception as e:  # pragma: no cover - ignore notif errors in tests
        current_app.logger.error(f"Error notificando cambio de estado: {e}")

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
        "archivo_url": getattr(ticket_obj, 'archivo_url', None),
        "latitud": getattr(ticket_obj, 'latitud', None),
        "longitud": getattr(ticket_obj, 'longitud', None)
    }
    return jsonify(ticket_data)

# ---------- CHAT EN VIVO: MENSAJES (SOLO TOKEN) ----------
@ticket_bp.route('/chat/<int:ticket_id>/mensajes', methods=['GET'])
@token_requerido
def get_chat_mensajes(current_user: User, ticket_id: int):
    """
    Devuelve los mensajes del chat en vivo para un ticket.
    Requiere que el usuario esté autenticado.
    """
    try:
        sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
        es_dueño_del_ticket = sala_de_chat.user_id == current_user.id

        log_ticket_debug("get_chat_mensajes", ticket_id, None, sala_de_chat)

        if sala_de_chat.user_id is None and not es_agente_municipal:
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

        if sala_de_chat.user_id is not None and not (es_agente_municipal or es_dueño_del_ticket):
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

        if sala_de_chat.estado == "cerrado" and not es_agente_municipal:
            return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

        ultimo_mensaje_id = request.args.get('ultimo_mensaje_id', default=0, type=int)
        mensajes_nuevos = (
            TicketComentario.query
            .filter(
                TicketComentario.municipio_ticket_id == ticket_id,
                TicketComentario.id > ultimo_mensaje_id
            )
            .order_by(TicketComentario.fecha.asc())
            .all()
        )
        mensajes_formateados = [
            {
                "id": msg.id,
                "texto": msg.comentario,
                "fecha": msg.fecha.isoformat(),
                "es_admin": msg.es_admin
            }
            for msg in mensajes_nuevos
        ]
        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados
        }
        return jsonify(respuesta_final)
    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500

# ---------- CHAT EN VIVO: RESPONDER CIUDADANO (SOLO TOKEN) ----------
@ticket_bp.route('/chat/<int:ticket_id>/responder_ciudadano', methods=['POST'])
@token_requerido
def responder_ciudadano_a_chat(current_user: User, ticket_id: int):
    """
    Permite al ciudadano responder en el chat de su ticket.
    Requiere que el usuario esté autenticado.
    """
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
    if not sala_de_chat:
        return jsonify({"error": "Sala de chat no encontrada."}), 404

    es_dueño = sala_de_chat.user_id == current_user.id

    log_ticket_debug("responder_ciudadano", ticket_id, None, sala_de_chat)

    if sala_de_chat.user_id is None or not es_dueño:
        return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

    if sala_de_chat.estado == "cerrado":
        return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    user_id_para_comentario = current_user.id if current_user else None

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id,
        tipo_ticket="municipio",
        comentario_data={
            "comentario": data["comentario"],
            "user_id": user_id_para_comentario,
            "es_admin": False
        }
    )
    if nuevo_comentario:
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- PANEL POR CATEGORÍA (AGENTES MUNICIPALES) ----------
@ticket_bp.route('/panel_por_categoria', methods=['GET'])
@token_requerido
def get_panel_por_categoria(current_user: User):
    es_agente_municipal = current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
    if not es_agente_municipal:
        return jsonify({"error": "No tienes permiso para acceder a este panel."}), 403

    try:
        tickets = (
            MunicipioTicket.query
            .order_by(MunicipioTicket.fecha.desc())
            .all()
        )
        tickets_agrupados = defaultdict(list)

        for ticket in tickets:
            direccion = ticket.direccion or "No especificada"
            if not ticket.direccion and ticket.detalles:
                for line in ticket.detalles.splitlines():
                    if "Dirección del problema:" in line:
                        direccion = line.split("Dirección del problema:")[1].strip()
                        break

            ticket_data = {
                "id": ticket.id,
                "tipo": "municipio",
                "nro_ticket": ticket.nro_ticket,
                "asunto": ticket.asunto,
                "estado": ticket.estado,
                "fecha": ticket.fecha.isoformat(),
                "direccion": direccion,
                "latitud": getattr(ticket, 'latitud', None),
                "longitud": getattr(ticket, 'longitud', None)
            }
            tickets_agrupados[ticket.categoria or "Sin Categoría"].append(ticket_data)

        return jsonify(tickets_agrupados)

    except Exception as e:
        current_app.logger.error(f"Error en get_panel_por_categoria: {e}", exc_info=True)
        return jsonify({"error": "Error interno al generar el panel de tickets."}), 500

# ---------- ACTUALIZAR UBICACIÓN DE TICKET ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/ubicacion', methods=['PUT', 'POST'])
@token_requerido
def actualizar_ubicacion_ticket(current_user: User, tipo: str, ticket_id: int):
    """Actualiza la ubicación geográfica asociada a un ticket."""
    data = request.get_json() or {}
    lat = (
        data.get('latitud')
        or data.get('lat')
        or data.get('latitude')
    )
    lon = (
        data.get('longitud')
        or data.get('lon')
        or data.get('lng')
        or data.get('longitude')
    )
    direccion = data.get('direccion')

    TicketModel = MunicipioTicket if tipo == 'municipio' else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # Refuerzo de permisos:
    if current_user.id == ticket_obj.user_id:
        pass
    elif tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios' and hasattr(current_user, "municipio_id") and ticket_obj.municipio_id == current_user.municipio_id:
        pass
    elif tipo == 'pyme' and current_user.rubro_id and getattr(ticket_obj, 'rubro_id', None) == current_user.rubro_id:
        pass
    else:
        return jsonify({"error": "No tienes permiso para modificar este ticket."}), 403

    log_ticket_debug(
        "actualizar_ubicacion",
        ticket_id,
        None,
        ticket_obj,
    )

    if lat is not None:
        try:
            ticket_obj.latitud = float(lat)
        except (TypeError, ValueError):
            current_app.logger.warning(f"Latitud inválida: {lat}")
    if lon is not None:
        try:
            ticket_obj.longitud = float(lon)
        except (TypeError, ValueError):
            current_app.logger.warning(f"Longitud inválida: {lon}")
    if direccion:
        ticket_obj.direccion = direccion

    db.session.commit()

    return jsonify({
        "id": ticket_obj.id,
        "latitud": ticket_obj.latitud,
        "longitud": ticket_obj.longitud,
        "direccion": ticket_obj.direccion
    })

# ---------- ENCUESTA DE SATISFACCION ----------
@ticket_bp.route('/<string:tipo>/<int:ticket_id>/encuesta', methods=['POST'])
@token_requerido
def enviar_encuesta(current_user: User, tipo: str, ticket_id: int):
    data = request.get_json(silent=True) or {}
    puntuacion = data.get('puntuacion')
    comentario = data.get('comentario')
    if puntuacion is None:
        return jsonify({"error": "Falta la puntuacion."}), 400

    TicketModel = MunicipioTicket if tipo == 'municipio' else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    es_dueño = ticket_obj.user_id == current_user.id
    es_admin = False
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios' and hasattr(current_user, "municipio_id") and ticket_obj.municipio_id == current_user.municipio_id:
        es_admin = True
    if tipo == 'pyme' and current_user.rubro_id and getattr(ticket_obj, 'rubro_id', None) == current_user.rubro_id:
        es_admin = True

    if not (es_dueño or es_admin):
        return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

    encuesta = servicio_tickets.guardar_encuesta(ticket_id, tipo, int(puntuacion), comentario)
    if encuesta:
        return jsonify({"success": True, "encuesta_id": encuesta.id})
    return jsonify({"error": "No se pudo guardar"}), 500

@ticket_bp.route('/<string:tipo>/<int:ticket_id>/encuesta', methods=['GET'])
@token_requerido
def obtener_encuesta(current_user: User, tipo: str, ticket_id: int):
    encuesta = TicketSatisfaccion.query.filter_by(ticket_id=ticket_id, tipo=tipo).first()
    if not encuesta:
        return jsonify({})
    # Permiso: solo dueño o admin/empleado de la empresa/municipio
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    es_dueño = ticket_obj and ticket_obj.user_id == current_user.id
    es_admin = False
    if tipo == 'municipio' and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios' and hasattr(current_user, "municipio_id") and ticket_obj and ticket_obj.municipio_id == current_user.municipio_id:
        es_admin = True
    if tipo == 'pyme' and current_user.rubro_id and ticket_obj and getattr(ticket_obj, 'rubro_id', None) == current_user.rubro_id:
        es_admin = True
    if not (es_dueño or es_admin):
        return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

    return jsonify({
        "ticket_id": encuesta.ticket_id,
        "tipo": encuesta.tipo,
        "puntuacion": encuesta.puntuacion,
        "comentario": encuesta.comentario,
        "fecha": encuesta.fecha.isoformat() if encuesta.fecha else None,
    })

# ---------- MAPA DE TICKETS ABIERTOS ----------
@ticket_bp.route('/<string:tipo>/mapa', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def mapa_de_tickets(current_user: User, tipo: str):
    """Devuelve los tickets abiertos con latitud y longitud solo para agentes de la empresa/municipio."""
    if tipo == "municipio":
        # Solo tickets municipales
        if not (current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'):
            return jsonify({"error": "No tienes permiso para ver este mapa."}), 403
        datos = servicio_tickets.obtener_tickets_abiertos_con_ubicacion(tipo)
    else:
        # Solo tickets de su empresa/rubro
        if not current_user.rubro_id:
            return jsonify({"error": "No tienes permiso para ver este mapa."}), 403
        datos = servicio_tickets.obtener_tickets_abiertos_con_ubicacion(tipo)
    return jsonify(datos)
