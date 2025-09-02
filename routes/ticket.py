import os
import uuid
import logging
from werkzeug.utils import secure_filename
from flask import Blueprint, request, jsonify, current_app, send_from_directory, render_template
from socket_service import emit_ticket_update
from models import (
    MunicipioTicket,
    PymeTicket,
    User,
    TicketComentario,
    TicketSatisfaccion,
    Conversacion,
    ArchivoAdjunto,
    db,
)
from datetime import datetime, timedelta
from services.ticket_service import servicio_tickets
from services.gcs_service import upload_to_gcs # Import the new GCS service
from utils.auth_helpers import token_requerido, anon_o_token_requerido, admin_o_empleado_requerido
from utils.permissions import require_role
from collections import defaultdict
from sqlalchemy import or_
from utils.ticket_utils import normalize_category
logger = logging.getLogger("app")

from utils.recaptcha import verify_recaptcha

ticket_bp = Blueprint('ticket_bp', __name__)

MENSAJE_CHAT_CERRADO = "El chat fue cerrado"
MENSAJE_SIN_PERMISOS = "No tienes permiso para acceder a este chat."

def guardar_archivo_adjunto_ticket(file_storage, user_id, ticket_id, tipo_ticket) -> ArchivoAdjunto | None:
    """
    Handles the upload of a file to GCS and creates an ArchivoAdjunto record.
    """
    if not file_storage or not file_storage.filename:
        return None

    # Use the centralized GCS upload function
    upload_result = upload_to_gcs(file_storage)

    if not upload_result:
        current_app.logger.error(f"GCS upload failed for ticket {tipo_ticket} {ticket_id}.")
        return None

    try:
        nuevo_adjunto = ArchivoAdjunto(
            user_id=user_id,
            filename=upload_result["unique_name"],
            nombre_original=upload_result["original_name"],
            mime=upload_result["mimetype"],
            tamano=upload_result["size"],
            tipo="adjunto_ticket_respuesta",
            url=upload_result["public_url"]
        )

        if tipo_ticket == "municipio":
            nuevo_adjunto.municipio_ticket_id = ticket_id
        elif tipo_ticket == "pyme":
            nuevo_adjunto.pyme_ticket_id = ticket_id
        else:
            current_app.logger.error(f"Tipo de ticket desconocido '{tipo_ticket}' al guardar adjunto.")
            # Here we might want to delete the GCS object if the ticket type is invalid
            return None

        db.session.add(nuevo_adjunto)
        # The commit will be handled by the calling function after all operations.
        return nuevo_adjunto
    except Exception as e:
        current_app.logger.error(f"Error creating ArchivoAdjunto record for ticket {tipo_ticket} {ticket_id}: {e}", exc_info=True)
        # Attempt to clean up the orphaned GCS object
        # (Requires a delete function in gcs_service, for now we log)
        current_app.logger.error(f"Orphaned GCS object may exist: {upload_result.get('unique_name')}")
        return None

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
from flask import redirect, url_for

def _generate_friendly_ticket_id(ticket, ticket_type_str):
    """Genera un ID de ticket amigable como M-992323 o P-123."""
    # Para MunicipioTicket, nro_ticket es un UUID string. Usamos los primeros 6 caracteres.
    prefix = "M" if ticket_type_str == "municipio" else "P"

    # El nro_ticket de MunicipioTicket ahora es un entero como el de Pyme,
    # así que podemos unificar la lógica.
    # El formato amigable será M-XXXXXX o P-XXXXXX.
    # Usamos el nro_ticket si existe y es un número, sino el id.
    ticket_number = getattr(ticket, 'nro_ticket', ticket.id)
    if not isinstance(ticket_number, (int, str)) or not str(ticket_number).isdigit():
        ticket_number = ticket.id

    return f"{prefix}-{ticket_number}"


def serialize_ticket_to_json(ticket, ticket_type):
    """
    Serializa un objeto de ticket a un diccionario JSON con el formato
    específico requerido por el frontend del panel de CRM.
    Incluye el historial de comentarios completo.
    """
    # Serializar todos los comentarios del ticket
    comentarios_serializados = []
    if ticket.comentarios:
        # Ordenar por fecha ascendente para mostrar el historial cronológicamente
        lista_comentarios = ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        comentarios_serializados = [c.to_dict() for c in lista_comentarios]

    # Reutilizar la lógica existente para obtener la información de contacto unificada
    # Esta función necesita el modelo User, que ya está importado en este archivo.
    user_data = _get_user_info(ticket, User)

    # El campo 'description' debe ser 'detalles' si existe, sino 'pregunta'.
    description = getattr(ticket, 'detalles', '') or getattr(ticket, 'pregunta', '')

    historial_chat = servicio_tickets.obtener_historial_chat(ticket)


    # Construir el diccionario con la estructura deseada
    dni_vecino = user_data.get("dni")
    if dni_vecino == "No especificado" or not dni_vecino:
        dni_vecino = None

    serialized_data = {
        "id": ticket.id,
        "tipo": ticket_type,
        "nro_ticket": _generate_friendly_ticket_id(ticket, ticket_type),
        "asunto": getattr(ticket, 'asunto', 'Sin Asunto'),
        # Mapear "cerrado" a "resuelto" para una mejor UX en el panel.
        "estado": "resuelto" if getattr(ticket, 'estado', None) == "cerrado" else ticket.estado,
        "fecha": ticket.fecha.isoformat(),
        "categoria": normalize_category(getattr(ticket, 'categoria', 'Sin Categoría')),
        "direccion": user_data.get("direccion", "No especificada"),
        "distrito": getattr(ticket, 'distrito', None),
        "latitud": getattr(ticket, 'latitud', None),
        "longitud": getattr(ticket, 'longitud', None),
        "nombre_usuario": user_data.get("nombre", "No especificado"),
        "email": user_data.get("email", "No especificado"),
        "telefono": user_data.get("telefono", "No especificado"),
        "dni": dni_vecino,
        "description": description,
        "channel": getattr(ticket, 'canal_ingreso', 'desconocido'),
        "comentarios": comentarios_serializados,
        "historial_chat": historial_chat,
        "informacion_personal_vecino": {
            "nombre": user_data.get("nombre", "No especificado"),
            "dni": dni_vecino,
            "direccion": user_data.get("direccion", "No especificada"),
            "email": user_data.get("email", "No especificado"),
            "telefono": user_data.get("telefono", "No especificado")
        }
    }
    return serialized_data


def get_tickets_del_usuario_logic(current_user: User):
    if not current_user:
        return jsonify({"error": "Usuario no asociado, no se pueden mostrar tickets."}), 404

    try:
        requested_estado_filter = request.args.get("estado")
        requested_categoria_filter = request.args.get("categoria")

        TicketModel = None
        tipo_ticket_str = '' # Para usar en la serialización

        if current_user.tipo_chat == "municipio":
            TicketModel = MunicipioTicket
            current_app.logger.info(f"[DEBUG] Usuario municipal: id={current_user.id}, municipio_id={current_user.municipio_id}, rol={current_user.rol}, tipo_chat={current_user.tipo_chat}")
            if not current_user.municipio_id:
                current_app.logger.error(f"[DEBUG] Usuario {current_user.id} no tiene municipio_id.")
                return jsonify({"error": "El usuario municipal no tiene asignado un municipio_id válido. Comuníquese con el soporte."}), 400

            query_base = TicketModel.query.filter(TicketModel.municipio_id == current_user.municipio_id)
            current_app.logger.info(f"[DEBUG] Querying for municipio_id: {current_user.municipio_id}")
            tipo_ticket_str = 'municipio'
        else:
            TicketModel = PymeTicket
            current_app.logger.info(f"[DEBUG] Usuario PYME: id={current_user.id}, rubro_id={current_user.rubro_id}, rol={current_user.rol}, tipo_chat={current_user.tipo_chat}")
            if current_user.rubro_id:
                query_base = TicketModel.query.filter(PymeTicket.rubro_id == current_user.rubro_id)
            else:
                current_app.logger.warning(f"Usuario PYME {current_user.id} sin rubro_id intentando acceder a /tickets")
                return jsonify({"error": "Usuario PYME no tiene rubro asignado o configuración incorrecta."}), 400
            tipo_ticket_str = 'pyme'

        # Aplicar filtro de categoría si se proveyó (afecta tanto al summary como a la lista)
        if requested_categoria_filter:
            if requested_categoria_filter.lower() == "luminarias":
                query_base = query_base.filter(TicketModel.categoria.ilike("%lumin%"))
            else:
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
                category_conditions = []
                for cat_name in employee_specific_categories:
                    if cat_name.lower() == "luminarias":
                        category_conditions.append(TicketModel.categoria.ilike("%lumin%"))
                    else:
                        category_conditions.append(TicketModel.categoria.ilike(cat_name))
                query_base = query_base.filter(or_(*category_conditions))


        # Obtener todos los tickets que cumplen con los filtros base (municipio/rubro y categoría de empleado/request) para el resumen
        all_tickets_for_summary_calculation = query_base.all()

        summary_by_status = defaultdict(int)
        defined_statuses = [
            "nuevo",
            "en_proceso",
            "en_vivo",
            "esperando_agente_en_vivo",
            "resuelto",
            "cerrado",
        ]

        # Inicializar todos los estados definidos en 0 para que el frontend
        # siempre reciba las claves esperadas aunque no existan tickets en ese
        # estado.
        for st in defined_statuses:
            summary_by_status[st] = 0

        for t_sum in all_tickets_for_summary_calculation:
            # El filtro de categoría de empleado ya se aplicó en la query_base
            estado_actual = "resuelto" if t_sum.estado == "cerrado" else t_sum.estado
            if estado_actual in defined_statuses:
                summary_by_status[estado_actual] += 1
            else:
                summary_by_status["otros"] += 1  # Contar otros estados
        summary_by_status["total"] = len(all_tickets_for_summary_calculation)
        # Unificar los tickets cerrados dentro de la cuenta de "resuelto" para
        # que el frontend los trate como reclamos resueltos.
        summary_by_status["resuelto"] += summary_by_status.get("cerrado", 0)

        # Ahora, obtener la lista de tickets para la página actual, aplicando el filtro de estado si existe
        current_app.logger.info(f"Filtros aplicados: estado={requested_estado_filter}, categoria={requested_categoria_filter}")
        final_tickets_query = query_base  # query_base ya tiene los filtros de categoria y rol

        search_query = request.args.get("q")
        if search_query:
            like_pattern = f"%{search_query}%"
            search_filters = [
                User.name.ilike(like_pattern),
                TicketModel.nro_ticket.ilike(like_pattern),
                TicketModel.estado.ilike(like_pattern),
            ]
            if hasattr(TicketModel, "nombre_vecino"):
                search_filters.append(TicketModel.nombre_vecino.ilike(like_pattern))
            if hasattr(TicketModel, "dni"):
                search_filters.append(TicketModel.dni.ilike(like_pattern))
            if hasattr(TicketModel, "dni_vecino"):
                search_filters.append(TicketModel.dni_vecino.ilike(like_pattern))
            final_tickets_query = (
                final_tickets_query
                .join(User, TicketModel.user_id == User.id)
                .filter(or_(*search_filters))
            )

        if requested_estado_filter and requested_estado_filter != 'todos':
            if requested_estado_filter == 'resuelto':
                final_tickets_query = final_tickets_query.filter(TicketModel.estado.in_(["resuelto", "cerrado"]))
            else:
                final_tickets_query = final_tickets_query.filter(TicketModel.estado == requested_estado_filter)

        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", current_app.config.get("TICKETS_PER_PAGE_DEFAULT", 50)))

        tickets_for_list_page = (
            final_tickets_query
            .order_by(TicketModel.fecha.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        serialized_tickets = [serialize_ticket_to_json(t, tipo_ticket_str) for t in tickets_for_list_page]

        # Devolver tanto la lista de tickets para la página actual como el resumen
        return jsonify({
            "tickets": serialized_tickets,
            "summary": summary_by_status
        })

    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

@ticket_bp.route('/tickets', methods=['GET'])
@ticket_bp.route('/tickets/', methods=['GET'])
@token_requerido
def get_tickets_del_usuario(current_user: User):
    if current_user.rol not in ['admin', 'empleado']:
        return redirect(url_for('ticket_bp.get_mis_tickets'))

    return get_tickets_del_usuario_logic(current_user)

# ---------- LISTA DE MIS TICKETS (cliente) ----------
@ticket_bp.route('/tickets/mios', methods=['GET'])
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
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", current_app.config.get("TICKETS_PER_PAGE_DEFAULT", 50)))

        tickets_muni = (
            query_muni.order_by(MunicipioTicket.fecha.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        tickets_pyme = (
            query_pyme.order_by(PymeTicket.fecha.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
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
                    "dni": getattr(t, "dni", None) or getattr(t, "dni_vecino", None),
                })
            return base

        todos = [serialize(t, "municipio") for t in tickets_muni] + [
            serialize(t, "pyme") for t in tickets_pyme
        ]
        todos.sort(key=lambda x: x["fecha"], reverse=True)

        # Mantener la consistencia con otros endpoints que devuelven un objeto
        # en lugar de una lista plana para facilitar la extensión en el
        # frontend.
        return jsonify({"tickets": todos})
    except Exception as e:
        current_app.logger.error(
            f"Error en get_mis_tickets para user {getattr(current_user,'id','?')}: {e}",
            exc_info=True,
        )
        return jsonify({"error": "Error interno al obtener tus tickets."}), 500

# ---------- DETALLE DE TICKET ----------
def _get_user_info(ticket, user_model):
    """
    Consolidate user info giving precedence to the data stored on the ticket
    itself. This ensures the panel displays the information provided when the
    ticket was created even if the user's profile has outdated values.
    """
    # 1. Initialize with None to clearly distinguish from empty strings
    user_info = {"nombre": None, "telefono": None, "email": None, "direccion": None, "dni": None, "descripcion": None}

    # 2. Start with the explicit data saved on the ticket
    user_info["nombre"] = getattr(ticket, 'nombre_vecino', None)
    user_info["telefono"] = getattr(ticket, 'telefono_vecino', None) or getattr(ticket, 'telefono', None)
    user_info["email"] = getattr(ticket, 'email_vecino', None) or getattr(ticket, 'email', None)
    user_info["direccion"] = getattr(ticket, 'direccion', None)
    user_info["dni"] = getattr(ticket, 'dni', None) or getattr(ticket, 'dni_vecino', None)

    # 3. Fill remaining data with the associated User model as fallback
    ticket_owner_user = db.session.get(user_model, ticket.user_id) if ticket.user_id else None
    if ticket_owner_user:
        user_info["nombre"] = user_info["nombre"] or ticket_owner_user.name
        user_info["telefono"] = user_info["telefono"] or ticket_owner_user.telefono
        user_info["email"] = user_info["email"] or ticket_owner_user.email
        user_info["direccion"] = user_info["direccion"] or getattr(ticket_owner_user, "direccion", None)
        user_info["dni"] = user_info["dni"] or getattr(ticket_owner_user, "dni", None)

    # 4. Fallback to 'detalles' field for any missing info
    detalles_texto = getattr(ticket, 'detalles', '') or ''
    user_info["descripcion"] = detalles_texto
    if detalles_texto:
        if not user_info["nombre"]:
            if "Nombre:" in detalles_texto: user_info["nombre"] = detalles_texto.split("Nombre:")[1].split("\n")[0].strip()
        if not user_info["telefono"]:
            if "Teléfono:" in detalles_texto: user_info["telefono"] = detalles_texto.split("Teléfono:")[1].split("\n")[0].strip()
        if not user_info["email"]:
            if "Email:" in detalles_texto: user_info["email"] = detalles_texto.split("Email:")[1].split("\n")[0].strip()
        if not user_info["direccion"]:
            if "Dirección:" in detalles_texto: user_info["direccion"] = detalles_texto.split("Dirección:")[1].split("\n")[0].strip()
        if not user_info["dni"]:
            if "DNI:" in detalles_texto: user_info["dni"] = detalles_texto.split("DNI:")[1].split("\n")[0].strip()

    # 5. Final cleanup: replace any remaining None/empty with "No especificado" for display
    for key, value in user_info.items():
        if not value: # Catches None and empty strings
            user_info[key] = "No especificado"

    return user_info

def _serialize_ticket_details(ticket, ticket_type):
    """Serializa los detalles de un ticket (municipio o pyme) a un diccionario JSON."""
    user_data = _get_user_info(ticket, User)

    comentarios = [c.to_dict() for c in ticket.comentarios]

    timeline = servicio_tickets.obtener_timeline_ticket(ticket)

    historial_chat = servicio_tickets.obtener_historial_chat(ticket)

    archivos_adjuntos_data = []
    if hasattr(ticket, 'archivos'):
        archivos_list = ticket.archivos.all() if hasattr(ticket.archivos, 'all') else ticket.archivos
        for adj in archivos_list:
            analisis_data = None
            if adj.analisis:
                analisis = adj.analisis
                analisis_data = {
                    "id": analisis.id, "resumen": analisis.resumen, "estado_analisis": analisis.estado_analisis,
                    "fecha_analisis": analisis.fecha_analisis.isoformat() if analisis.fecha_analisis else None,
                    "error_analisis": analisis.error_analisis, "texto_extraido": analisis.texto_extraido,
                    "datos_estructurados": analisis.datos_estructurados, "tipo_analisis": analisis.tipo_analisis,
                }
            archivos_adjuntos_data.append({
                "id": adj.id, "name": adj.nombre_original or adj.filename, "mimeType": adj.mime,
                "size": adj.tamano, "url": adj.url, "fecha": adj.fecha.isoformat() if adj.fecha else None,
                "analisis": analisis_data
            })

    informacion_personal = {
            "nombre": user_data["nombre"],
            "telefono": user_data["telefono"],
            "email": user_data["email"],
            "direccion": user_data["direccion"],
            "dni": user_data["dni"]
        }

    ticket_data = {
        "id": ticket.id,
        "id_ticket": _generate_friendly_ticket_id(ticket, ticket_type),
        "tipo": ticket_type,
        "nro_ticket_original": ticket.nro_ticket, # Mantenemos el nro original por si acaso
        "asunto": getattr(ticket, 'asunto', ''),
        "categoria_reclamo": getattr(ticket, 'categoria', ''),
        "estado_ticket": ticket.estado,
        "fecha_hora_creacion": ticket.fecha.isoformat(),
        "descripcion_completa_reclamo": getattr(ticket, 'pregunta', ''),
        "detalles_adicionales": user_data["descripcion"], # Datos extraídos del campo 'detalles'
        "comentarios": sorted(comentarios, key=lambda c: c['fecha']),
        "nombre_completo_solicitante": user_data["nombre"],
        "telefono_contacto": user_data["telefono"],
        "mail_contacto": user_data["email"],
        "dni": user_data["dni"],
        "direccion_exacta_aproximada": user_data["direccion"],
        "archivos_adjuntos": archivos_adjuntos_data,
        "ubicacion_geografica": {
            "latitud": getattr(ticket, 'latitud', None),
            "longitud": getattr(ticket, 'longitud', None),
        },
        "canal_ingreso": getattr(ticket, 'canal_ingreso', None),
        "contacto_seguimiento": getattr(ticket, 'contacto_seguimiento', None),
        "nombre_y_avatar_whatsapp": {
            "nombre": getattr(ticket, 'nombre_display_whatsapp', None),
            "avatar_url": getattr(ticket, 'url_avatar_whatsapp', None),
        },
        "informacion_personal_vecino": informacion_personal,
        "historial_chat": historial_chat,
        "timeline": timeline,
    }
    return ticket_data


@ticket_bp.route('/tickets/municipio/por_numero/<string:nro_ticket>', methods=['GET'])
@anon_o_token_requerido
def get_ticket_by_number_public(current_user, owner_user, anon_id, nro_ticket: str):
    """Consulta un ticket municipal por su número."""

    normalizado = str(nro_ticket).upper()
    if normalizado.startswith("M-"):
        normalizado = normalizado.split("-", 1)[1]

    ticket = None
    if current_user:
        # Petición autenticada: no requiere PIN ni reCAPTCHA
        ticket = MunicipioTicket.query.filter_by(nro_ticket=normalizado).first()
    else:
        pin = request.args.get("pin")
        if not pin:
            return jsonify({"error": "PIN requerido."}), 400
        token = request.args.get("recaptcha_token")
        if token and token.lower() not in ("undefined", "null"):
            if not verify_recaptcha(token):
                return jsonify({"error": "Verificación reCAPTCHA fallida."}), 400

        ticket = MunicipioTicket.query.filter_by(nro_ticket=normalizado, consulta_pin=pin).first()

    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    ticket_data = _serialize_ticket_details(ticket, "municipio")
    return jsonify(ticket_data)

@ticket_bp.route('/tickets/municipio/<int:ticket_id>', methods=['GET'])
@token_requerido
def get_ticket_details(current_user: User, ticket_id: int):
    """
    Devuelve el detalle de un ticket municipal, verificando que el usuario
    (admin o empleado) pertenezca al municipio correcto.
    """
    if current_user.tipo_chat != "municipio" or not current_user.municipio_id:
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    ticket = db.session.get(MunicipioTicket, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    if ticket.municipio_id != current_user.municipio_id:
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    ticket_data = _serialize_ticket_details(ticket, "municipio")
    return jsonify(ticket_data)

@ticket_bp.route('/tickets/pyme/<int:ticket_id>', methods=['GET'])
@token_requerido
def get_ticket_details_pyme(current_user: User, ticket_id: int):
    """
    Devuelve el detalle de un ticket de pyme, verificando que el usuario
    (admin o empleado) pertenezca a la pyme correcta.
    """
    if current_user.tipo_chat != "pyme" or not current_user.rubro_id:
        return jsonify({"error": "Acceso denegado. Se requiere un usuario de pyme."}), 403

    ticket = db.session.get(PymeTicket, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket no encontrado."}), 404

    if ticket.rubro_id != current_user.rubro_id:
        return jsonify({"error": "No tienes permiso para ver este ticket."}), 403

    ticket_data = _serialize_ticket_details(ticket, "pyme")
    return jsonify(ticket_data)

# ---------- RESPONDER A TICKET (AGENTE) ----------
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/responder', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def responder_a_ticket(current_user: User, tipo: str, ticket_id: int):
    comentario_texto = None
    archivos_subidos = []

    if request.content_type.startswith('application/json'):
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({"error": "Formato JSON inválido"}), 400
        comentario_texto = data.get("comentario")
        attachment_info = data.get("attachment_info")
        archivos_subidos = [] # No files in JSON payload
        current_app.logger.info(f"Admin response via JSON: text='{comentario_texto}', attachment_info={attachment_info}")
    elif request.content_type.startswith('multipart/form-data'):
        comentario_texto = request.form.get("comentario")
        archivos_subidos = request.files.getlist("archivos") # 'archivos' es el name del input type="file"
        current_app.logger.info(f"Admin response via multipart: text='{comentario_texto}', files_count={len(archivos_subidos)}")
    else:
        current_app.logger.warning(f"Admin response con Content-Type no soportado: {request.content_type}")
        return jsonify({"error": "Unsupported Content-Type. Use application/json or multipart/form-data."}), 415

    # Default comentario_texto to empty string if it's None *before* the check
    if comentario_texto is None:
        comentario_texto = ""

    # Now check if there's actual content (non-whitespace text or any files)
    if not comentario_texto.strip() and not archivos_subidos:
        return jsonify({"error": "El comentario o al menos un archivo son requeridos."}), 400
    
    # comentario_texto is now guaranteed to be a string (potentially empty or whitespace only if not stripped yet for saving)


    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    if ticket_obj.estado == "cerrado":
        return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    # Refuerzo de permisos:
    if tipo == 'municipio':
        if not (
            current_user.tipo_chat == "municipio" and
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
        "responder_agente_con_archivos", # Acción actualizada
        ticket_id,
        request.headers.get("X-Anon-Id") or request.headers.get("Anon-Id"),
        ticket_obj,
    )

    # Process file uploads first to get their IDs
    archivos_adjuntados_db = []
    if archivos_subidos:
        for file_storage in archivos_subidos:
            if file_storage and file_storage.filename:
                adjunto_db = guardar_archivo_adjunto_ticket(file_storage, current_user.id, ticket_id, tipo)
                if adjunto_db:
                    archivos_adjuntados_db.append(adjunto_db)
                else:
                    current_app.logger.error(f"No se pudo guardar uno de los archivos para el ticket {ticket_id}.")
                    # If a file fails, we might want to stop, but for now, we'll continue and report at the end.

    # Now create comments
    comentarios_creados = []
    # Create a comment for the text part, if it exists
    if comentario_texto and comentario_texto.strip():
        comentario_obj = servicio_tickets.crear_comentario(
            ticket_id=ticket_id, tipo_ticket=tipo,
            comentario_data={"comentario": comentario_texto, "user_id": current_user.id, "es_admin": True}
        )
        if comentario_obj:
            comentarios_creados.append(comentario_obj)
        else:
            current_app.logger.error(f"No se pudo guardar el comentario de texto para el ticket {ticket_id}.")

    # If the request was JSON and had attachment_info, create a comment for it
    if 'attachment_info' in locals() and attachment_info:
        file_comment_text = f"[Archivo adjunto: {attachment_info.get('name', 'archivo')}]"
        file_comment_obj = servicio_tickets.crear_comentario(
            ticket_id=ticket_id, tipo_ticket=tipo,
            comentario_data={
                "comentario": file_comment_text,
                "user_id": current_user.id,
                "es_admin": True,
                "archivo_adjunto_id": attachment_info.get('id')
            }
        )
        if file_comment_obj:
            comentarios_creados.append(file_comment_obj)
        else:
            current_app.logger.error(f"No se pudo crear el comentario para el archivo adjunto ID: {attachment_info.get('id')}.")

    # Create a separate comment for each physically attached file (from multipart)
    for adjunto in archivos_adjuntados_db:
        db.session.add(adjunto)
        db.session.flush()

        file_comment_text = f"[Archivo adjunto: {adjunto.nombre_original}]"
        file_comment_obj = servicio_tickets.crear_comentario(
            ticket_id=ticket_id, tipo_ticket=tipo,
            comentario_data={
                "comentario": file_comment_text,
                "user_id": current_user.id,
                "es_admin": True,
                "archivo_adjunto_id": adjunto.id
            }
        )
        if file_comment_obj:
            comentarios_creados.append(file_comment_obj)
        else:
            current_app.logger.error(f"No se pudo crear el comentario para el archivo adjunto ID: {adjunto.id}.")

    # Check if anything was successfully created
    if not comentarios_creados and not archivos_adjuntados_db:
        return jsonify({"error": "No se pudo guardar la respuesta (ni comentario ni archivos)."}), 500

    try:
        if ticket_obj.estado == "nuevo" and (comentario_texto.strip() or archivos_adjuntados_db): # Si hay nuevo contenido (texto o archivos)
            ticket_obj.estado = "en_proceso"
        
        db.session.commit() # Commit después de todas las operaciones (comentario y archivos)
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al hacer commit final para respuesta de ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al finalizar la respuesta."}), 500

    # --- Notificaciones ---
    # Construir el mensaje de notificación. Si hay texto, usarlo. Si solo hay archivos, un mensaje genérico.
    mensaje_notificacion_base = comentario_texto if comentario_texto.strip() else "Se han adjuntado nuevos archivos a tu ticket."

    # El objeto 'ticket_obj' ya está cargado.
    # 'archivos_adjuntados_db' es la lista de objetos ArchivoAdjunto recién creados y guardados.
    try:
        from services.email_service import (
            enviar_email_ticket_novedad,
            enviar_sms_ticket_novedad,
            enviar_whatsapp_ticket_novedad,
        )
        # Email siempre se envía si hay email
        enviar_email_ticket_novedad(ticket_obj, mensaje_notificacion_base) # TODO: Email con adjuntos? Por ahora solo texto.

        # SMS siempre se envía si hay teléfono (solo texto)
        enviar_sms_ticket_novedad(ticket_obj, mensaje_notificacion_base)

        # WhatsApp con adjuntos (si los hay)
        if tipo == "municipio": # Asumiendo que WhatsApp es principalmente para municipio por ahora
            enviar_whatsapp_ticket_novedad(ticket_obj, mensaje_notificacion_base, archivos_adjuntos=archivos_adjuntados_db)
        current_app.logger.info(f"Notificaciones para respuesta de ticket {ticket_id} (tipo {tipo}) procesadas.")

        # Notificación por Websocket/Pusher
        # Serializar el ticket completo para enviar todos los datos actualizados
        ticket_json = serialize_ticket_to_json(ticket_obj, tipo)
        emit_ticket_update(ticket_json)


    except Exception as e_notif:
        current_app.logger.error(f"Error durante el envío de notificaciones para respuesta de ticket {ticket_id}: {e_notif}", exc_info=True)
        # No devolver error al cliente por fallo en notificaciones, ya que el ticket/comentario se guardó.

    # --- Preparar respuesta JSON ---
    # La función detalle_ticket ya serializa los archivos, así que podemos reusar esa lógica
    # o simplemente devolver el ticket actualizado.
    
    # Recargar comentarios y archivos para la respuesta
    # (La relación 'comentarios' y 'archivos' en ticket_obj se actualiza tras el commit)
    comentarios_actualizados = [{"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin} for c in ticket_obj.comentarios.order_by(TicketComentario.fecha.asc()).all()]
    
    archivos_actualizados_data = []
    if hasattr(ticket_obj, 'archivos'):
        # Asegurarse de que los archivos recién añadidos estén en la sesión y se carguen
        # db.session.expire(ticket_obj, ['archivos']) # Forzar recarga de la relación si es necesario
        # O simplemente consultar de nuevo:
        archivos_list = ArchivoAdjunto.query.filter(
            (ArchivoAdjunto.municipio_ticket_id == ticket_id) if tipo == "municipio" else (ArchivoAdjunto.pyme_ticket_id == ticket_id)
        ).all()

        for adj in archivos_list: # Usar la lista recién consultada
            # La lógica de análisis no aplica para respuestas de agentes por ahora
            archivos_actualizados_data.append({
                "id": adj.id,
                "name": adj.nombre_original or adj.filename,
                "mimeType": adj.mime,
                "size": adj.tamano,
                "url": adj.url, 
                "fecha": adj.fecha.isoformat() if adj.fecha else None,
                "analisis": None 
            })

    ticket_data_respuesta = {
        "id": ticket_obj.id, "tipo": tipo, "nro_ticket": ticket_obj.nro_ticket,
        "asunto": getattr(ticket_obj, 'asunto', ''), "estado": ticket_obj.estado,
        "fecha": ticket_obj.fecha.isoformat(),
        "detalles": getattr(ticket_obj, 'detalles', getattr(ticket_obj, 'pregunta', '')),
        "comentarios": comentarios_actualizados, # Usar la lista actualizada
        "archivos_adjuntos": archivos_actualizados_data, # Usar la lista actualizada
        # ... (otros campos de ticket_obj si son necesarios en la respuesta)
    }
    return jsonify(ticket_data_respuesta), 200

# ---------- CAMBIAR ESTADO DE TICKET ----------
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/estado', methods=['PUT'])
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
            current_user.tipo_chat == "municipio" and
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
        request.headers.get("X-Anon-Id") or request.headers.get("Anon-Id"),
        ticket_obj,
    )

    ticket_obj.estado = nuevo_estado
    if nuevo_estado == "cerrado":
        encuesta = TicketSatisfaccion(
            ticket_id=ticket_obj.id,
            tipo=tipo,
            puntuacion=5,
            comentario="Cierre automático",
        )
        db.session.add(encuesta)
    comentario_estado = TicketComentario(
        municipio_ticket_id=ticket_obj.id if tipo == "municipio" else None,
        pyme_ticket_id=ticket_obj.id if tipo == "pyme" else None,
        comentario=f"Estado actualizado a '{nuevo_estado}'",
        user_id=current_user.id,
        es_admin=True,
        origen="sistema",
        estado_ticket=nuevo_estado,
    )
    db.session.add(comentario_estado)
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

    # Notificación por Websocket
    ticket_json = serialize_ticket_to_json(ticket_obj, tipo)
    emit_ticket_update(ticket_json)

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
@ticket_bp.route('/tickets/chat/<int:ticket_id>/mensajes', methods=['GET'])
@anon_o_token_requerido
def get_chat_mensajes(current_user: User, ticket_id: int, anon_id: str = None, owner_user: User = None):
    """
    Devuelve los mensajes del chat en vivo para un ticket.
    Requiere que el usuario esté autenticado o que proporcione un anon_id válido.
    """
    try:
        sala_de_chat = db.session.get(MunicipioTicket, ticket_id)
        if not sala_de_chat:
            return jsonify({"error": "Sala de chat no encontrada."}), 404

        es_agente_municipal = current_user and current_user.tipo_chat == "municipio"
        es_dueño_del_ticket = current_user and sala_de_chat.user_id == current_user.id
        es_anon_valido = anon_id and sala_de_chat.anon_id == anon_id

        log_ticket_debug("get_chat_mensajes", ticket_id, anon_id, sala_de_chat)

        if not (es_agente_municipal or es_dueño_del_ticket or es_anon_valido):
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
        # Usar el método to_dict() del modelo para asegurar que todos los datos,
        # incluyendo la información de adjuntos, se serialicen correctamente.
        mensajes_formateados = [msg.to_dict() for msg in mensajes_nuevos]

        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados,
        }
        return jsonify(respuesta_final)
    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500


# ---------- CHAT EN VIVO PYME: MENSAJES ----------
@ticket_bp.route('/tickets/chat/pyme/<int:ticket_id>/mensajes', methods=['GET'])
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

        # Usar el método to_dict() del modelo para asegurar que todos los datos,
        # incluyendo la información de adjuntos, se serialicen correctamente.
        mensajes_formateados = [msg.to_dict() for msg in mensajes_nuevos]

        # Devolver una estructura consistente con get_chat_mensajes
        respuesta_final = {
            "estado_chat": sala_de_chat.estado,
            "mensajes": mensajes_formateados,
        }
        return jsonify(respuesta_final)
    except Exception as e:
        current_app.logger.error(f"Error en get_chat_mensajes_pyme para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los mensajes del chat."}), 500

# ---------- TIMELINE DEL TICKET ----------
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/timeline', methods=['GET'])
@anon_o_token_requerido
def get_ticket_timeline(current_user: User, tipo: str, ticket_id: int, anon_id: str = None, owner_user: User = None):
    """Devuelve la línea de tiempo de un ticket con mensajes y cambios de estado.

    Además de la timeline basada en comentarios y modificaciones de estado,
    ahora se incluye el historial de conversación asociado al ``anon_id`` del
    ticket. Esto permite que el frontend muestre una vista completa del flujo de
    interacción del reclamo o pedido, combinando mensajes del chat y estados.
    """
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket if tipo == "pyme" else None
    if not TicketModel:
        return jsonify({"error": f"Tipo de ticket no válido: {tipo}"}), 400

    ticket_obj = db.session.get(TicketModel, ticket_id)
    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    if tipo == "municipio":
        es_agente = current_user and current_user.tipo_chat == "municipio"
        es_dueno = current_user and ticket_obj.user_id == current_user.id
        es_anon = anon_id and ticket_obj.anon_id == anon_id
        if not (es_agente or es_dueno or es_anon):
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403
        if ticket_obj.estado == "cerrado" and not es_agente:
            return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403
    else:  # pyme
        es_agente = current_user and current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id
        es_dueno = current_user and ticket_obj.user_id == current_user.id
        es_anon = anon_id and ticket_obj.anon_id == anon_id
        if not (es_agente or es_dueno or es_anon):
            return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403
        if ticket_obj.estado == "cerrado" and not es_agente:
            return jsonify({"error": MENSAJE_CHAT_CERRADO}), 403

    timeline = servicio_tickets.obtener_timeline_ticket(ticket_obj)
    historial_chat = servicio_tickets.obtener_historial_chat(ticket_obj)

    return jsonify({
        "estado_chat": ticket_obj.estado,
        "timeline": timeline,
        "historial_chat": historial_chat,
    })

# ---------- CHAT EN VIVO: RESPONDER CIUDADANO (SOLO TOKEN) ----------
@ticket_bp.route('/tickets/chat/<int:ticket_id>/responder_ciudadano', methods=['POST'])
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
        db.session.commit()
        # Notificación por Websocket
        data = {
            "message": f"Nuevo mensaje en tu ticket #{sala_de_chat.nro_ticket}",
            "ticket_id": ticket_id,
            "tipo": "municipio",
            "comentario": nuevo_comentario.to_dict()
        }
        emit_ticket_update(data)
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- CHAT EN VIVO PYME: RESPONDER CLIENTE ----------
@ticket_bp.route('/tickets/chat/pyme/<int:ticket_id>/responder_cliente', methods=['POST'])
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
        db.session.commit()
        data = {
            "message": f"El estado de tu ticket #{sala_de_chat.nro_ticket} ha sido actualizado a: '{sala_de_chat.estado}'.",
            "ticket_id": ticket_id,
            "tipo": "pyme",
            "nuevo_estado": sala_de_chat.estado
        }
        emit_ticket_update(data)
        return jsonify({"success": True, "mensaje_id": nuevo_comentario.id}), 201

    return jsonify({"error": "No se pudo guardar la respuesta."}), 500

# ---------- PANEL POR CATEGORÍA (AGENTES MUNICIPALES) ----------
@ticket_bp.route('/tickets/panel_por_categoria', methods=['GET'])
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

        if current_user.tipo_chat != "municipio":
            return jsonify({"error": "Acceso denegado."}), 403

        query = MunicipioTicket.query.filter(MunicipioTicket.municipio_id == current_user.municipio_id)

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
        defined_statuses = ["nuevo", "en_proceso", "en_vivo", "esperando_agente_en_vivo", "cerrado"]

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

                user_data = _get_user_info(ticket_obj, User)
                ticket_data_serialized = {
                    "id": ticket_obj.id, "tipo": "municipio", "nro_ticket": ticket_obj.nro_ticket,
                    "asunto": ticket_obj.asunto, "estado": ticket_obj.estado,
                    "fecha": ticket_obj.fecha.isoformat(), "direccion": direccion,
                    "latitud": getattr(ticket_obj, 'latitud', None), "longitud": getattr(ticket_obj, 'longitud', None),
                    "nombre_usuario": user_data["nombre"],
                    "telefono": user_data["telefono"],
                    "email_usuario": user_data["email"],
                    "dni": user_data["dni"],
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
@ticket_bp.route('/tickets/panel_pyme', methods=['GET'])
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
        defined_statuses = ["nuevo", "en_proceso", "en_vivo", "esperando_agente_en_vivo", "cerrado"]

        for categoria_key, tickets_in_category_list in tickets_grouped_by_cat.items():
            summary_by_status_for_cat = defaultdict(int)
            serialized_tickets_for_cat = []

            for ticket_obj in tickets_in_category_list:
                if ticket_obj.estado in defined_statuses:
                    summary_by_status_for_cat[ticket_obj.estado] += 1
                else:
                    summary_by_status_for_cat["otros"] += 1
                summary_by_status_for_cat["total"] = summary_by_status_for_cat.get("total", 0) + 1

                user_data = _get_user_info(ticket_obj, User)
                ticket_data_serialized = {
                    "id": ticket_obj.id, "tipo": "pyme", "nro_ticket": ticket_obj.nro_ticket,
                    "asunto": ticket_obj.asunto, "estado": ticket_obj.estado,
                    "fecha": ticket_obj.fecha.isoformat(),
                    "direccion": getattr(ticket_obj, 'direccion', None),
                    "latitud": getattr(ticket_obj, 'latitud', None), "longitud": getattr(ticket_obj, 'longitud', None),
                     # PYME specific fields for serialization if needed by frontend for this view
                    "nombre_usuario": user_data["nombre"],
                    "telefono": user_data["telefono"],
                    "email_usuario": user_data["email"],
                    "dni": user_data["dni"],
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

# ---------- PANEL UNIFICADO ----------
@ticket_bp.route('/tickets/panel', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def get_ticket_panel(current_user: User):
    """Retorna el panel de tickets según el tipo de chat del usuario."""
    if current_user.tipo_chat == "municipio":
        return get_panel_por_categoria(current_user)
    else:
        return get_panel_pyme(current_user)

# ---------- ACTUALIZAR UBICACIÓN DE TICKET ----------
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/ubicacion', methods=['PUT', 'POST'])
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

    anon_id_header = request.headers.get("X-Anon-Id") or request.headers.get("Anon-Id")

    # Si el ticket aún es anónimo pero coincide el X-Anon-Id, lo asignamos al usuario
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
    elif tipo == 'municipio' and current_user.tipo_chat == "municipio" and ticket_obj.municipio_id == current_user.municipio_id:
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
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/encuesta', methods=['POST'])
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
    if tipo == 'municipio' and current_user.tipo_chat == "municipio" and ticket_obj.municipio_id == current_user.municipio_id:
        es_admin = True
    if tipo == 'pyme' and current_user.rubro_id and getattr(ticket_obj, 'rubro_id', None) == current_user.rubro_id:
        es_admin = True

    if not (es_dueño or es_admin):
        return jsonify({"error": MENSAJE_SIN_PERMISOS}), 403

    encuesta = servicio_tickets.guardar_encuesta(ticket_id, tipo, int(puntuacion), comentario)
    if encuesta:
        return jsonify({"success": True, "encuesta_id": encuesta.id})
    return jsonify({"error": "No se pudo guardar"}), 500

@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/encuesta', methods=['GET'])
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
    if tipo == 'municipio' and current_user.tipo_chat == "municipio" and ticket_obj.municipio_id == current_user.municipio_id:
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
@ticket_bp.route('/tickets/<string:tipo>/mapa', methods=['GET'])
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

    logger.info(
        "[MAPA_TICKETS] tipo=%s user_id=%s filtros: fecha_inicio=%s fecha_fin=%s categoria=%s estado=%s",
        tipo,
        getattr(current_user, "id", None),
        fecha_inicio,
        fecha_fin,
        categoria,
        estado,
    )

    if tipo == "municipio":
        if not (current_user.tipo_chat == "municipio"):
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
    logger.info(
        "[MAPA_TICKETS] puntos_retornados=%s ejemplo=%s",
        len(datos),
        datos[:3] if datos else [],
    )

    # Convert the aggregated points to a GeoJSON FeatureCollection for MapLibre
    features = [
        {
            "type": "Feature",
            "properties": {
                "weight": punto.get("weight", 1),
                "categoria": punto.get("categoria"),
            },
            "geometry": {
                "type": "Point",
                "coordinates": [
                    punto.get("location", {}).get("lng"),
                    punto.get("location", {}).get("lat"),
                ],
            },
        }
        for punto in datos
        if punto.get("location")
    ]

    return jsonify({"type": "FeatureCollection", "features": features})

# ---------- ENVIAR HISTORIAL POR CORREO ----------
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>/send-history', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def send_ticket_history(current_user: User, tipo: str, ticket_id: int):
    """
    Recupera el historial completo de un ticket y lo envía por correo electrónico
    al cliente y al correo de contacto del agente/municipio.
    """
    TicketModel = MunicipioTicket if tipo == "municipio" else PymeTicket
    ticket_obj = db.session.get(TicketModel, ticket_id)

    if not ticket_obj:
        return jsonify({"error": "Ticket no encontrado."}), 404

    # --- Verificación de Permisos ---
    if tipo == 'municipio':
        if not (current_user.tipo_chat == "municipio" and ticket_obj.municipio_id == current_user.municipio_id):
            return jsonify({"error": "No tienes permiso para realizar esta acción."}), 403
    elif tipo == 'pyme':
        if not (current_user.rubro_id and ticket_obj.rubro_id == current_user.rubro_id):
            return jsonify({"error": "No tienes permiso para realizar esta acción."}), 403
    else:
        return jsonify({"error": f"Tipo de ticket no válido: {tipo}"}), 400

    # --- Recopilación de Datos ---
    try:
        comentarios = ticket_obj.comentarios.order_by(TicketComentario.fecha.asc()).all()

        adjuntos_unicos = []
        adjunto_ids = set()
        for c in comentarios:
            if c.archivo_adjunto and c.archivo_adjunto.id not in adjunto_ids:
                adjuntos_unicos.append(c.archivo_adjunto)
                adjunto_ids.add(c.archivo_adjunto.id)

        # --- Obtener Destinatarios ---
        cliente_info = _get_user_info(ticket_obj, User)
        email_cliente = cliente_info.get("email") if cliente_info.get("email") != "No especificado" else None

        email_agente = None
        if tipo == 'municipio' and ticket_obj.municipio_id:
            agente_user = db.session.get(User, ticket_obj.municipio_id)
            if agente_user:
                email_agente = agente_user.email
        elif tipo == 'pyme' and ticket_obj.rubro_id:
            # Asumiendo que el rubro tiene un usuario asociado o una forma de encontrar el email
            # Por ahora, usamos el email del usuario que realiza la acción como fallback.
            email_agente = current_user.email

        destinos = [d for d in [email_cliente, email_agente] if d]
        if not destinos:
            return jsonify({"error": "No se encontraron correos de destino válidos para el cliente o el agente."}), 400

        # --- Renderizar y Enviar Correo ---
        asunto = f"Historial de conversación del Ticket #{ticket_obj.nro_ticket}"

        cuerpo_html = render_template(
            "email/ticket_history.html",
            ticket=ticket_obj,
            comentarios=comentarios
        )

        from services.email_service import enviar_email_con_multiples_adjuntos

        exito = enviar_email_con_multiples_adjuntos(
            destinos=destinos,
            asunto=asunto,
            cuerpo_html=cuerpo_html,
            adjuntos=adjuntos_unicos
        )

        if exito:
            return jsonify({"success": True, "message": "El historial del ticket ha sido enviado por correo."})
        else:
            return jsonify({"error": "Hubo un problema al enviar el correo con el historial."}), 500

    except Exception as e:
        current_app.logger.error(f"Error en send_ticket_history para ticket {ticket_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al procesar el envío del historial."}), 500


# The local file serving route is no longer needed as files are served from GCS public URLs.
# from flask_login import login_required, current_user as flask_login_current_user

# @ticket_bp.route('/tickets/archivos/<filename>', methods=['GET'])
# @login_required
# def get_ticket_adjunto(filename):
#     # This logic is now obsolete. Access control should be handled by the main
#     # /archivos/<filename> route if a centralized download point is needed,
#     # or by ensuring GCS URLs are not easily guessable if direct access is allowed.
#     # For simplicity, we rely on the main /archivos endpoint.
#     return jsonify({"error": "This endpoint is deprecated."}), 410

@ticket_bp.route('/tickets/panel', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def ticket_panel(current_user: User):
    """Renderiza el panel de tickets."""
    return send_from_directory('static', 'ticket_panel.html')
