from flask import Blueprint, request, jsonify, current_app
from models import (
    MunicipioTicket,
    PymeTicket,
    User,
    TicketComentario,
    TicketSatisfaccion,
    db,
)
from datetime import datetime, timedelta
from services.ticket_service import servicio_tickets
from .auth import token_requerido, anon_o_token_requerido, admin_o_empleado_requerido
from utils.permissions import require_role
from collections import defaultdict

ticket_bp = Blueprint('ticket_bp', __name__, url_prefix='/tickets')

MENSAJE_CHAT_CERRADO = "El chat fue cerrado"
MENSAJE_SIN_PERMISOS = "No tienes permiso para acceder a este chat."

def log_ticket_debug(action: str, ticket_id: int, header_anon_id: str | None, ticket_obj) -> None:
    """Registro unificado de acciones sobre tickets."""
    log_message = (
        f"{action} | ticket_id={ticket_id} | "
        f"header_anon_id={header_anon_id} | "
        f"ticket_anon_id={getattr(ticket_obj, 'anon_id', None)} | "
        f"estado_actual={getattr(ticket_obj, 'estado', None)}"
    )
    # Si la acción es un cambio de estado, podríamos querer loguear el estado al que se cambió.
    # Esto requeriría pasar el nuevo_estado a esta función, o loguearlo directamente en cambiar_estado_ticket.
    # Por ahora, mantenemos el log como está, pero es una consideración para el futuro.
    current_app.logger.info(log_message)

# ---------- LISTA DE TICKETS (logueado) ----------
@ticket_bp.route('/', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_tickets_del_usuario(current_user: User):
    if not current_user or not current_user.rubro:
        return jsonify({"error": "Usuario o rubro no asociado, no se pueden mostrar tickets."}), 404

    try:
        requested_estado_filter = request.args.get("estado")
        requested_categoria_filter = request.args.get("categoria")

        TicketModel = None
        base_query_filters = []
        tipo_ticket_str = '' # Para usar en la serialización

        # Definir función de serialización genérica primero
        def serialize_ticket_func(t, ticket_type_str):
            data = {
                "id": t.id, "tipo": ticket_type_str, "nro_ticket": t.nro_ticket,
                "asunto": getattr(t, 'asunto', 'N/A'), "estado": t.estado,
                "fecha": t.fecha.isoformat(), "categoria": getattr(t, 'categoria', None),
                "direccion": getattr(t, 'direccion', None),
                "latitud": getattr(t, 'latitud', None), "longitud": getattr(t, 'longitud', None)
            }
            if ticket_type_str == 'pyme':
                data.update({
                    "telefono": getattr(t, 'telefono', None),
                    "email": getattr(t, 'email', None),
                    "dni": getattr(t, 'dni', None),
                    "estado_cliente": getattr(t, 'estado_cliente', None),
                })
            return data

        if current_user.rubro.nombre.lower().strip() == 'municipios':
            TicketModel = MunicipioTicket
            base_query_filters.append(MunicipioTicket.municipio_id == current_user.municipio_id)
            tipo_ticket_str = 'municipio'
        else: # PYME
            TicketModel = PymeTicket
            if current_user.rubro_id:
                base_query_filters.append(PymeTicket.rubro_id == current_user.rubro_id)
            else:
                current_app.logger.warning(f"Usuario PYME {current_user.id} sin rubro_id intentando acceder a /tickets")
                return jsonify({"error": "Usuario PYME no tiene rubro asignado o configuración incorrecta."}), 400
            tipo_ticket_str = 'pyme'

        # Construir la query base
        query_base = TicketModel.query.filter(*base_query_filters)

        # Aplicar filtro de categoría si se proveyó (afecta tanto al summary como a la lista)
        if requested_categoria_filter:
            query_base = query_base.filter(TicketModel.categoria == requested_categoria_filter)

        # Aplicar filtro de categorías asignadas al empleado (afecta tanto al summary como a la lista)
        employee_specific_categories = []
        if current_user.rol == 'empleado' and current_user.ticket_categorias:
            employee_specific_categories = [c.strip().lower() for c in current_user.ticket_categorias.split(',') if c.strip()]
            if employee_specific_categories:
                 # Usar ilike para búsquedas insensibles a mayúsculas/minúsculas si es necesario,
                 # o asumir que las categorías se guardan normalizadas.
                 # Por ahora, se asume que la comparación directa es suficiente si las categorías están normalizadas.
                 # query_base = query_base.filter(TicketModel.categoria.in_(employee_specific_categories))
                 # SQLAlchemy no tiene un `ANY` directo como SQL puro para listas de strings de esta forma.
                 # Se puede usar OR:
                from sqlalchemy import or_
                category_conditions = [TicketModel.categoria.ilike(cat_name) for cat_name in employee_specific_categories]
                query_base = query_base.filter(or_(*category_conditions))


        # Obtener todos los tickets que cumplen con los filtros base (municipio/rubro y categoría de empleado/request) para el resumen
        all_tickets_for_summary_calculation = query_base.all()

        summary_by_status = defaultdict(int)
        defined_statuses = ["nuevo", "en_proceso", "cerrado"]

        for t_sum in all_tickets_for_summary_calculation:
            # El filtro de categoría de empleado ya se aplicó en la query_base
            if t_sum.estado in defined_statuses:
                summary_by_status[t_sum.estado] += 1
            else:
                summary_by_status["otros"] += 1 # Contar otros estados
        summary_by_status["total"] = len(all_tickets_for_summary_calculation)

        # Ahora, obtener la lista de tickets para la página actual, aplicando el filtro de estado si existe
        final_tickets_query = query_base # query_base ya tiene los filtros de categoria y rol
        if requested_estado_filter:
            final_tickets_query = final_tickets_query.filter(TicketModel.estado == requested_estado_filter)

        tickets_for_list_page = final_tickets_query.order_by(TicketModel.fecha.desc()).all()

        serialized_tickets = [serialize_ticket_func(t, tipo_ticket_str) for t in tickets_for_list_page]

        return jsonify({
            "summary_by_status": dict(summary_by_status),
            "tickets": serialized_tickets
        })

    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

# ---------- LISTA DE MIS TICKETS (cliente) ----------
@ticket_bp.route('/mios', methods=['GET'])
@token_requerido
def get_mis_tickets(current_user: User):
    """Devuelve solo los tickets asociados al usuario autenticado."""
    try:
        estado = request.args.get("estado")
        categoria = request.args.get("categoria")
        query_muni = MunicipioTicket.query.filter_by(user_id=current_user.id)
        query_pyme = PymeTicket.query.filter_by(user_id=current_user.id)
        if estado:
            query_muni = query_muni.filter_by(estado=estado)
            query_pyme = query_pyme.filter_by(estado=estado)
        if categoria:
            query_muni = query_muni.filter(MunicipioTicket.categoria == categoria)
            query_pyme = query_pyme.filter(PymeTicket.categoria == categoria)
        tickets_muni = query_muni.order_by(MunicipioTicket.fecha.desc()).all()
        tickets_pyme = query_pyme.order_by(PymeTicket.fecha.desc()).all()

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
            enviar_whatsapp_ticket_novedad, # <--- IMPORTAR NUEVA FUNCIÓN
        )
        mensaje_notificacion = f"El estado de tu ticket #{ticket_obj.nro_ticket} ha sido actualizado a: '{nuevo_estado}'."

        enviar_email_ticket_novedad(ticket_obj, mensaje_notificacion)
        enviar_sms_ticket_novedad(ticket_obj, mensaje_notificacion)
        if tipo == "municipio": # Por ahora, WhatsApp solo para municipio
            enviar_whatsapp_ticket_novedad(ticket_obj, mensaje_notificacion)

    except Exception as e:  # pragma: no cover - ignore notif errors in tests
        current_app.logger.error(f"Error notificando cambio de estado para ticket {ticket_id} (tipo {tipo}): {e}", exc_info=True)

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


# ---------- CHAT EN VIVO PYME: MENSAJES ----------
@ticket_bp.route('/chat/pyme/<int:ticket_id>/mensajes', methods=['GET'])
@token_requerido
def get_chat_mensajes_pyme(current_user: User, ticket_id: int):
    """Devuelve los mensajes del chat en vivo para una pyme."""
    try:
        sala_de_chat = db.session.get(PymeTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        es_agente_pyme = current_user.rubro_id and sala_de_chat.rubro_id == current_user.rubro_id
        es_dueño = sala_de_chat.user_id == current_user.id

        log_ticket_debug("get_chat_mensajes_pyme", ticket_id, None, sala_de_chat)

        if sala_de_chat.user_id is None and not es_agente_pyme:
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

        if sala_de_chat.user_id is not None and not (es_agente_pyme or es_dueño):
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

        if sala_de_chat.estado == "cerrado" and not es_agente_pyme:
            return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

        ultimo_mensaje_id = request.args.get('ultimo_mensaje_id', default=0, type=int)
        mensajes_nuevos = (
            TicketComentario.query
            .filter(
                TicketComentario.pyme_ticket_id == ticket_id,
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

        return jsonify({"estado_chat": sala_de_chat.estado, "mensajes": mensajes_formateados})
    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes_pyme para ticket {ticket_id}: {e}", exc_info=True)
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

# ---------- CHAT EN VIVO PYME: RESPONDER CLIENTE ----------
@ticket_bp.route('/chat/pyme/<int:ticket_id>/responder_cliente', methods=['POST'])
@token_requerido
def responder_cliente_a_chat(current_user: User, ticket_id: int):
    """Permite al cliente responder en el chat de su pyme."""
    data = request.get_json()
    if not data or not data.get("comentario"):
        return jsonify({"error": "El comentario no puede estar vacío."}), 400

    sala_de_chat = db.session.get(PymeTicket, ticket_id)
    if not sala_de_chat:
        return jsonify({"error": "Sala de chat no encontrada."}), 404

    es_dueño = sala_de_chat.user_id == current_user.id

    log_ticket_debug("responder_cliente_pyme", ticket_id, None, sala_de_chat)

    if sala_de_chat.user_id is None or not es_dueño:
        return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

    if sala_de_chat.estado == "cerrado":
        return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    nuevo_comentario = servicio_tickets.crear_comentario(
        ticket_id=ticket_id,
        tipo_ticket="pyme",
        comentario_data={
            "comentario": data["comentario"],
            "user_id": current_user.id,
            "es_admin": False,
        },
    )
    if nuevo_comentario:
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- PANEL POR CATEGORÍA (AGENTES MUNICIPALES) ----------
@ticket_bp.route('/panel_por_categoria', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def get_panel_por_categoria(current_user: User):

    try:
        from datetime import timedelta

        # Helper function (puede moverse a un archivo de utils o services después)
        def _calculate_ticket_metrics_for_list(ticket_list_with_comments):
            first_response_times = []
            resolution_times = []

            for ticket in ticket_list_with_comments:
                # Asegurarse que ticket.fecha es datetime object
                if not isinstance(ticket.fecha, datetime): # pragma: no cover
                    try:
                        # Intentar parsear si es string, o skip si no es válido
                        ticket.fecha = datetime.fromisoformat(str(ticket.fecha))
                    except ValueError:
                        continue # Skip este ticket si la fecha no es válida

                admin_comments = sorted([c for c in ticket.comentarios if c.es_admin], key=lambda c: c.fecha)

                if admin_comments:
                    first_admin_comment_time = admin_comments[0].fecha
                    if isinstance(first_admin_comment_time, datetime) and isinstance(ticket.fecha, datetime):
                        response_delta = first_admin_comment_time - ticket.fecha
                        first_response_times.append(response_delta.total_seconds())

                if ticket.estado == 'cerrado':
                    closure_time = ticket.ultima_actividad
                    # Asegurarse que closure_time y ticket.fecha son datetime
                    if not isinstance(closure_time, datetime): # pragma: no cover
                         closure_time = datetime.fromisoformat(str(closure_time)) if closure_time else ticket.fecha # fallback

                    if isinstance(closure_time, datetime) and isinstance(ticket.fecha, datetime):
                        resolution_delta = closure_time - ticket.fecha
                        resolution_times.append(resolution_delta.total_seconds())

            avg_first_response_seconds = sum(first_response_times) / len(first_response_times) if first_response_times else None
            avg_resolution_seconds = sum(resolution_times) / len(resolution_times) if resolution_times else None

            return {
                "avg_first_response_seconds": round(avg_first_response_seconds, 2) if avg_first_response_seconds is not None else None,
                "avg_resolution_seconds": round(avg_resolution_seconds, 2) if avg_resolution_seconds is not None else None,
                "responded_tickets_count": len(first_response_times),
                "resolved_tickets_count": len(resolution_times)
            }

        query = MunicipioTicket.query  # Comments will be loaded lazily

        if getattr(current_user, "municipio_id", None):
            query = query.filter_by(municipio_id=current_user.municipio_id)

        all_tickets_for_user_municipio = query.order_by(MunicipioTicket.fecha.desc()).all()

        # Filtrar por categorías de empleado DESPUÉS de cargar todos los tickets del municipio (con comentarios)
        # para que el cálculo de métricas generales (si se quisiera) no se vea afectado.
        # O bien, aplicar el filtro de empleado ANTES si las métricas deben ser solo sobre sus categorías.
        # Por ahora, las métricas serán por categoría, y el empleado solo verá las categorías asignadas.

        tickets_to_process = all_tickets_for_user_municipio
        if current_user.rol == 'empleado' and current_user.ticket_categorias:
            employee_allowed_categories = [c.strip().lower() for c in current_user.ticket_categorias.split(',') if c.strip()]
            tickets_to_process = [t for t in all_tickets_for_user_municipio if (t.categoria or '').lower() in employee_allowed_categories]

        # Agrupar tickets por categoría
        tickets_grouped_by_cat = defaultdict(list)
        for t_obj in tickets_to_process:
            tickets_grouped_by_cat[t_obj.categoria or "Sin Categoría"].append(t_obj)

        final_panel_data = {}
        defined_statuses = ["nuevo", "en_proceso", "cerrado"]

        for categoria_key, tickets_in_category_list in tickets_grouped_by_cat.items():
            summary_by_status_for_cat = defaultdict(int)
            serialized_tickets_for_cat = []

            for ticket_obj in tickets_in_category_list:
                if ticket_obj.estado in defined_statuses:
                    summary_by_status_for_cat[ticket_obj.estado] += 1
                else:
                    summary_by_status_for_cat["otros"] += 1
                summary_by_status_for_cat["total"] = summary_by_status_for_cat.get("total", 0) + 1

                direccion = ticket_obj.direccion or "No especificada"
                if not ticket_obj.direccion and ticket_obj.detalles:
                    for line in ticket_obj.detalles.splitlines():
                        if "Dirección del problema:" in line:
                            direccion = line.split("Dirección del problema:")[1].strip()
                            break

                ticket_data_serialized = {
                    "id": ticket_obj.id, "tipo": "municipio", "nro_ticket": ticket_obj.nro_ticket,
                    "asunto": ticket_obj.asunto, "estado": ticket_obj.estado,
                    "fecha": ticket_obj.fecha.isoformat(), "direccion": direccion,
                    "latitud": getattr(ticket_obj, 'latitud', None), "longitud": getattr(ticket_obj, 'longitud', None)
                }
                serialized_tickets_for_cat.append(ticket_data_serialized)

            category_metrics = _calculate_ticket_metrics_for_list(tickets_in_category_list)

            final_panel_data[categoria_key] = {
                "summary_by_status": dict(summary_by_status_for_cat),
                "metrics": category_metrics,
                "tickets": serialized_tickets_for_cat,
            }
        panel_list = [
            {"categoria": cat, **data} for cat, data in final_panel_data.items()
        ]
        return jsonify(panel_list)

    except Exception as e:
        current_app.logger.error(f"Error en get_panel_por_categoria: {e}", exc_info=True)
        return jsonify({"error": "Error interno al generar el panel de tickets."}), 500

# ---------- PANEL PYME (AGENTES PYME) ----------
@ticket_bp.route('/panel_pyme', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def get_panel_pyme(current_user: User):
    try:
        query = PymeTicket.query
        if current_user.rubro_id:
            query = query.filter_by(rubro_id=current_user.rubro_id)
        tickets = query.order_by(PymeTicket.fecha.desc()).all()
        # Re-using _calculate_ticket_metrics_for_list defined above in get_panel_por_categoria
        # from datetime import datetime, timedelta # Ensure datetime is available

        query = PymeTicket.query  # Comments will be loaded lazily

        if current_user.rubro_id:
            query = query.filter_by(rubro_id=current_user.rubro_id)
        # else: # Should not happen for a PYME admin/employee if setup is correct
            # return jsonify({"error": "Rubro no asignado al usuario PYME."}), 400

        all_tickets_for_user_pyme = query.order_by(PymeTicket.fecha.desc()).all()

        tickets_to_process = all_tickets_for_user_pyme
        if current_user.rol == 'empleado' and current_user.ticket_categorias:
            employee_allowed_categories = [c.strip().lower() for c in current_user.ticket_categorias.split(',') if c.strip()]
            tickets_to_process = [t for t in all_tickets_for_user_pyme if (t.categoria or '').lower() in employee_allowed_categories]

        tickets_grouped_by_cat = defaultdict(list)
        for t_obj in tickets_to_process:
            tickets_grouped_by_cat[t_obj.categoria or "Sin Categoría"].append(t_obj)

        final_panel_data = {}
        defined_statuses = ["nuevo", "en_proceso", "cerrado"]

        for categoria_key, tickets_in_category_list in tickets_grouped_by_cat.items():
            summary_by_status_for_cat = defaultdict(int)
            serialized_tickets_for_cat = []

            for ticket_obj in tickets_in_category_list:
                if ticket_obj.estado in defined_statuses:
                    summary_by_status_for_cat[ticket_obj.estado] += 1
                else:
                    summary_by_status_for_cat["otros"] += 1
                summary_by_status_for_cat["total"] = summary_by_status_for_cat.get("total", 0) + 1

                ticket_data_serialized = {
                    "id": ticket_obj.id, "tipo": "pyme", "nro_ticket": ticket_obj.nro_ticket,
                    "asunto": ticket_obj.asunto, "estado": ticket_obj.estado,
                    "fecha": ticket_obj.fecha.isoformat(),
                    "direccion": getattr(ticket_obj, 'direccion', None),
                    "latitud": getattr(ticket_obj, 'latitud', None), "longitud": getattr(ticket_obj, 'longitud', None),
                     # PYME specific fields for serialization if needed by frontend for this view
                    "telefono": getattr(ticket_obj, 'telefono', None),
                    "email": getattr(ticket_obj, 'email', None),
                }
                serialized_tickets_for_cat.append(ticket_data_serialized)

            # Assuming _calculate_ticket_metrics_for_list is accessible here
            # (defined in the same file or imported)
            category_metrics = _calculate_ticket_metrics_for_list(tickets_in_category_list)

            final_panel_data[categoria_key] = {
                "summary_by_status": dict(summary_by_status_for_cat),
                "metrics": category_metrics,
                "tickets": serialized_tickets_for_cat,
            }

        panel_list = [
            {"categoria": cat, **data} for cat, data in final_panel_data.items()
        ]
        return jsonify(panel_list)
    except Exception as e:
        current_app.logger.error(f"Error en get_panel_pyme: {e}", exc_info=True)
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

    anon_id_header = request.headers.get("Anon-Id")

    # Si el ticket aún es anónimo pero coincide el Anon-Id, lo asignamos al usuario
    if (
        anon_id_header
        and ticket_obj.anon_id
        and ticket_obj.user_id is None
        and ticket_obj.anon_id == anon_id_header
    ):
        current_app.logger.info(
            "Asignando ticket %s del anon_id %s al usuario %s por ubicacion",
            ticket_id,
            anon_id_header,
            current_user.id,
        )
        ticket_obj.user_id = current_user.id

    # Refuerzo de permisos:
    if current_user.id == ticket_obj.user_id:
        pass
    elif (
        anon_id_header
        and ticket_obj.anon_id
        and ticket_obj.anon_id == anon_id_header
    ):
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
    """
    Devuelve los datos de tickets para visualización en mapa (puntos o calor).
    Los datos se agrupan por ubicación y se cuenta el número de tickets (peso).
    Permite filtrar por fecha_inicio, fecha_fin y categoria.
    """
    fecha_inicio = request.args.get("fecha_inicio")
    fecha_fin = request.args.get("fecha_fin")
    categoria = request.args.get("categoria")
    estado = request.args.get("estado") # Nuevo filtro de estado

    if tipo == "municipio":
        if not (
            current_user.rubro
            and current_user.rubro.nombre.lower().strip() == "municipios"
            and hasattr(current_user, "municipio_id")
        ):
            return jsonify({"error": "No tienes permiso para ver este mapa."}), 403

        # Consider renaming 'obtener_tickets_abiertos_con_ubicacion' if it now handles various states
        datos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa( # Asumiendo que se renombra/modifica el servicio
            tipo_ticket=tipo,
            municipio_id=current_user.municipio_id,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            categoria=categoria,
            estado=estado # Pasar el nuevo filtro
        )
    elif tipo == "pyme":
        if not current_user.rubro_id: # Asumimos que si es pyme, debe tener rubro_id
            return jsonify({"error": "No tienes permiso para ver este mapa."}), 403

        datos = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa( # Asumiendo que se renombra/modifica el servicio
            tipo_ticket=tipo,
            rubro_id=current_user.rubro_id,
            fecha_inicio=fecha_inicio,
            fecha_fin=fecha_fin,
            categoria=categoria,
            estado=estado # Pasar el nuevo filtro
        )
    else:
        return jsonify({"error": "Tipo de mapa no válido."}), 400

    return jsonify(datos)
