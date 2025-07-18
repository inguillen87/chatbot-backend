import os
import uuid
from werkzeug.utils import secure_filename
from flask import Blueprint, request, jsonify, current_app, send_from_directory
from models import (
    MunicipioTicket,
    PymeTicket,
    User,
    TicketComentario,
    TicketSatisfaccion,
    ArchivoAdjunto, # Asegurarse que ArchivoAdjunto esté importado
    db,
)
from datetime import datetime, timedelta
from services.ticket_service import servicio_tickets
from .auth import token_requerido, anon_o_token_requerido, admin_o_empleado_requerido
from utils.permissions import require_role
from collections import defaultdict

ticket_bp = Blueprint('ticket_bp', __name__)

# Carpeta para adjuntos de tickets
TICKET_ATTACHMENT_FOLDER = os.path.join(os.getcwd(), "data", "archivos_tickets")
os.makedirs(TICKET_ATTACHMENT_FOLDER, exist_ok=True)

MENSAJE_CHAT_CERRADO = "El chat fue cerrado"
MENSAJE_SIN_PERMISOS = "No tienes permiso para acceder a este chat."

def guardar_archivo_adjunto_ticket(file_storage, user_id, ticket_id, tipo_ticket) -> ArchivoAdjunto | None:
    if not file_storage or not file_storage.filename:
        return None

    try:
        original_filename = secure_filename(file_storage.filename)
        extension = os.path.splitext(original_filename)[1].lower()
        # Podríamos añadir una validación de extensiones aquí si es necesario
        # ALLOWED_TICKET_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".doc", ".docx", ".txt", ".xls", ".xlsx"}
        # if extension not in ALLOWED_TICKET_EXTENSIONS:
        #     current_app.logger.warning(f"Intento de subir archivo con extensión no permitida: {extension}")
        #     return None # O lanzar una excepción específica

        unique_filename = f"{uuid.uuid4().hex}{extension}"
        save_path = os.path.join(TICKET_ATTACHMENT_FOLDER, unique_filename)
        
        file_storage.save(save_path)
        file_size = os.path.getsize(save_path)

        # Crear la URL. Esto dependerá de cómo se sirvan los archivos.
        # Asumiré una ruta /tickets/archivos/<filename> que habrá que crear.
        file_url = f"/tickets/archivos/{unique_filename}" 

        nuevo_adjunto = ArchivoAdjunto(
            user_id=user_id, # El ID del agente que sube el archivo
            filename=unique_filename,
            nombre_original=original_filename,
            mime=file_storage.mimetype,
            tamano=file_size,
            tipo="adjunto_ticket_respuesta", # Un tipo para diferenciarlo de otros usos de ArchivoAdjunto
            url=file_url
        )

        if tipo_ticket == "municipio":
            nuevo_adjunto.municipio_ticket_id = ticket_id
        elif tipo_ticket == "pyme":
            nuevo_adjunto.pyme_ticket_id = ticket_id
        else:
            current_app.logger.error(f"Tipo de ticket desconocido '{tipo_ticket}' al guardar adjunto.")
            os.remove(save_path) # Limpiar archivo guardado si hay error
            return None

        db.session.add(nuevo_adjunto)
        # El commit se hará después de procesar todos los archivos y el comentario.
        return nuevo_adjunto
    except Exception as e:
        current_app.logger.error(f"Error al guardar archivo adjunto para ticket {tipo_ticket} {ticket_id}: {e}", exc_info=True)
        # Si hay un path guardado y ocurre un error, intentar borrarlo
        if 'save_path' in locals() and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception as e_remove:
                current_app.logger.error(f"Error al limpiar archivo {save_path} tras error: {e_remove}")
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
@ticket_bp.route('/tickets/', methods=['GET'])
@token_requerido
def get_tickets_redirect(current_user: User):
    """
    Redirects to the correct tickets list based on user role.
    """
    if current_user.rol in ['admin', 'empleado']:
        return get_tickets_del_usuario(current_user)
    else:
        return get_mis_tickets(current_user)

@ticket_bp.route('/tickets', methods=['GET'])
@token_requerido
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
        final_tickets_query = query_base  # query_base ya tiene los filtros de categoria y rol
        if requested_estado_filter:
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

        serialized_tickets = [serialize_ticket_func(t, tipo_ticket_str) for t in tickets_for_list_page]

        return jsonify(serialized_tickets)

    except Exception as e:
        current_app.logger.error(f"Error en get_tickets_del_usuario para user {getattr(current_user,'id','?')}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al obtener los tickets."}), 500

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
@ticket_bp.route('/tickets/<string:tipo>/<int:ticket_id>', methods=['GET'])
@anon_o_token_requerido
def detalle_ticket(current_user, tipo, ticket_id, anon_id=None, owner_user=None):
    """
    Devuelve el detalle de un ticket, reforzando la lógica de permisos para admins, empleados y usuarios.
    """
    anon_id = anon_id or request.headers.get("Anon-Id")
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
    nombre_final_usuario = "No especificado"
    telefono_final_usuario = "No especificado"
    email_final_usuario = "No especificado"
    
    ticket_owner_user = None
    if ticket.user_id:
        ticket_owner_user = db.session.get(User, ticket.user_id)
        if ticket_owner_user:
            nombre_final_usuario = ticket_owner_user.name or nombre_final_usuario
            # Usar el teléfono del perfil del usuario si está disponible
            if ticket_owner_user.telefono:
                 telefono_final_usuario = ticket_owner_user.telefono
            # Usar el email del perfil del usuario si está disponible
            if ticket_owner_user.email:
                email_final_usuario = ticket_owner_user.email

    # Si después de chequear ticket_owner_user, los datos siguen "No especificado" o estaban vacíos en el perfil,
    # intentar con los campos directos del ticket (nombre_vecino, etc.)
    # Esto es especialmente útil para tickets anónimos (ticket.user_id es None)
    # o si el User objeto no tiene los datos de contacto, o para priorizar datos del ticket.

    if nombre_final_usuario == "No especificado" or not nombre_final_usuario.strip():
        if hasattr(ticket, 'nombre_vecino') and ticket.nombre_vecino and ticket.nombre_vecino.strip():
            nombre_final_usuario = ticket.nombre_vecino
    
    if telefono_final_usuario == "No especificado" or not telefono_final_usuario.strip():
        if hasattr(ticket, 'telefono_vecino') and ticket.telefono_vecino and ticket.telefono_vecino.strip(): # Para MunicipioTicket
            telefono_final_usuario = ticket.telefono_vecino
        elif hasattr(ticket, 'telefono') and ticket.telefono and ticket.telefono.strip(): # Para PymeTicket
            telefono_final_usuario = ticket.telefono

    if email_final_usuario == "No especificado" or not email_final_usuario.strip():
        if hasattr(ticket, 'email_vecino') and ticket.email_vecino and ticket.email_vecino.strip(): # Para MunicipioTicket
            email_final_usuario = ticket.email_vecino
        elif hasattr(ticket, 'email') and ticket.email and ticket.email.strip(): # Para PymeTicket
            email_final_usuario = ticket.email

    # Fallback final a la extracción desde el campo 'detalles' si todavía no se encontraron y son "No especificado".
    detalles_texto = getattr(ticket, 'detalles', '') or ''
    if (nombre_final_usuario == "No especificado" or not nombre_final_usuario.strip()) and "Nombre:" in detalles_texto:
        nombre_final_usuario = detalles_texto.split("Nombre:")[1].split("\n")[0].strip()
    if (telefono_final_usuario == "No especificado" or not telefono_final_usuario.strip()) and "Teléfono:" in detalles_texto:
        telefono_final_usuario = detalles_texto.split("Teléfono:")[1].split("\n")[0].strip()
    if (email_final_usuario == "No especificado" or not email_final_usuario.strip()) and "Email:" in detalles_texto:
        email_final_usuario = detalles_texto.split("Email:")[1].split("\n")[0].strip()

    # Asegurarse de que si después de todo siguen siendo "No especificado", se envíe eso o None/null.
    # El frontend espera string, así que "No especificado" está bien si no hay dato.
    # O podrías cambiarlo a None aquí si el frontend lo maneja mejor. Por ahora, se mantiene "No especificado".

    direccion = getattr(ticket, 'direccion', None) or "No especificada"
    if (direccion == "No especificada" or not direccion.strip()) and "Dirección:" in detalles_texto:
        direccion = detalles_texto.split("Dirección:")[1].split("\n")[0].strip()
    elif ticket_owner_user and (direccion == "No especificada" or not direccion.strip()) and ticket_owner_user.direccion:
        direccion = ticket_owner_user.direccion


    comentarios = [
        {"id": c.id, "comentario": c.comentario, "fecha": c.fecha.isoformat(), "es_admin": c.es_admin}
        for c in ticket.comentarios
    ]

    # Serializar archivos adjuntos
    archivos_adjuntos_data = []
    if hasattr(ticket, 'archivos'):
        archivos_list = ticket.archivos.all() if hasattr(ticket.archivos, 'all') else ticket.archivos
        for adj in archivos_list:
            analisis_data = None
            if adj.analisis: # adj.analisis es la relación one-to-one con AnalisisArchivo
                analisis = adj.analisis
                analisis_data = {
                    "id": analisis.id,
                    "resumen": analisis.resumen,
                    "estado_analisis": analisis.estado_analisis,
                    "fecha_analisis": analisis.fecha_analisis.isoformat() if analisis.fecha_analisis else None,
                    "error_analisis": analisis.error_analisis,
                    "texto_extraido": analisis.texto_extraido,
                    "datos_estructurados": analisis.datos_estructurados, # Esto es JSON, el frontend lo parseará
                    "tipo_analisis": analisis.tipo_analisis,
                }

            archivos_adjuntos_data.append({
                "id": adj.id,
                "name": adj.nombre_original or adj.filename,
                "mimeType": adj.mime,
                "size": adj.tamano,
                "url": adj.url, # URL para descargar/ver el archivo original
                "fecha": adj.fecha.isoformat() if adj.fecha else None,
                "analisis": analisis_data # Incluir los datos del análisis
            })

    ticket_data = {
        "id": ticket.id,
        "tipo": tipo,
        "nro_ticket": ticket.nro_ticket,
        "asunto": getattr(ticket, 'asunto', ''),
        "categoria": getattr(ticket, 'categoria', ''),
        "estado": ticket.estado,
        "fecha": ticket.fecha.isoformat(),
        "pregunta": getattr(ticket, 'pregunta', ''),
        "detalles": detalles_texto, # Se sigue enviando el campo 'detalles' original por si se usa en otro lado
        "comentarios": sorted(comentarios, key=lambda c: c['fecha']),
        "nombre_usuario": nombre_final_usuario, # CORREGIDO
        "telefono": telefono_final_usuario,     # CORREGIDO (clave 'telefono' como espera el frontend)
        "email_usuario": email_final_usuario,   # CORREGIDO (clave 'email_usuario' como espera el frontend)
        "direccion": direccion,           # Dato obtenido de ticket.direccion, User.direccion o fallback
        "archivos_adjuntos": archivos_adjuntos_data,
        "latitud": getattr(ticket, 'latitud', None),
        "longitud": getattr(ticket, 'longitud', None)
    }
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
        comentario_texto = data.get("comentario")
        # No files expected in JSON payload for this simplified handling
        current_app.logger.info(f"Admin response via JSON: {comentario_texto}")
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
        "responder_agente_con_archivos", # Acción actualizada
        ticket_id,
        request.headers.get("Anon-Id"),
        ticket_obj,
    )

    nuevo_comentario_obj = None
    if comentario_texto: # Solo crear comentario si hay texto
        nuevo_comentario_obj = servicio_tickets.crear_comentario(
            ticket_id=ticket_id, tipo_ticket=tipo,
            comentario_data={"comentario": comentario_texto, "user_id": current_user.id, "es_admin": True}
        )
        if not nuevo_comentario_obj:
             # Si falla la creación del comentario (y era requerido), podría ser un error 500
            if not archivos_subidos: # Si no hay archivos, el comentario era lo único
                 return jsonify({"error": "No se pudo guardar el comentario."}), 500
            # Si hay archivos, continuamos para intentar guardarlos, pero logueamos el fallo del comentario
            current_app.logger.error(f"No se pudo guardar el comentario para el ticket {ticket_id}, pero se procederá con los archivos.")


    archivos_adjuntados_db = []
    if archivos_subidos:
        for file_storage in archivos_subidos:
            if file_storage and file_storage.filename: # Verificar que hay un archivo real
                adjunto_db = guardar_archivo_adjunto_ticket(file_storage, current_user.id, ticket_id, tipo)
                if adjunto_db:
                    archivos_adjuntados_db.append(adjunto_db)
                else:
                    # Si un archivo falla, ¿deberíamos detener todo o continuar?
                    # Por ahora, continuaremos pero informaremos. Podría ser un error parcial.
                    current_app.logger.error(f"No se pudo guardar uno de los archivos para el ticket {ticket_id}.")
                    # Podríamos devolver un error específico si NINGÚN archivo se pudo guardar y no hay comentario
                    if not comentario_texto and not any(archivos_adjuntados_db):
                         return jsonify({"error": "No se pudo guardar el comentario ni los archivos adjuntos."}), 500
    
    if not nuevo_comentario_obj and not archivos_adjuntados_db:
        # Esto podría pasar si el comentario está vacío y la subida de todos los archivos falló.
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

        es_agente_municipal = current_user and current_user.rubro and current_user.rubro.nombre.lower().strip() == 'municipios'
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

# También se necesitará una ruta para servir los archivos.
from flask_login import login_required, current_user as flask_login_current_user # Importar para Flask-Login

@ticket_bp.route('/tickets/archivos/<filename>', methods=['GET'])
@login_required # Usar login_required de Flask-Login
def get_ticket_adjunto(filename): # current_user ahora vendrá de flask_login_current_user
    current_user = flask_login_current_user # Obtener el usuario de Flask-Login
    # Validar filename para evitar directory traversal
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return jsonify({"error": "Nombre de archivo no válido."}), 400

    # Verificar permisos: ¿Quién puede acceder a este archivo?
    # 1. El usuario que lo subió (current_user.id == archivo.user_id)
    # 2. Si el archivo está asociado a un ticket, el dueño del ticket o un admin/empleado con permiso al ticket.
    archivo_obj = ArchivoAdjunto.query.filter_by(filename=safe_filename).first()
    if not archivo_obj:
        return jsonify({"error": "Archivo no encontrado."}), 404

    # Lógica de permisos (simplificada, podría necesitar ser más robusta):
    puede_acceder = False
    if archivo_obj.user_id == current_user.id: # El que lo subió
        puede_acceder = True
    else:
        ticket_id_asociado = archivo_obj.municipio_ticket_id or archivo_obj.pyme_ticket_id
        tipo_ticket_asociado = "municipio" if archivo_obj.municipio_ticket_id else "pyme"
        
        if ticket_id_asociado:
            TicketModel = MunicipioTicket if tipo_ticket_asociado == "municipio" else PymeTicket
            ticket_asociado = db.session.get(TicketModel, ticket_id_asociado)
            if ticket_asociado:
                if ticket_asociado.user_id == current_user.id: # Dueño del ticket
                    puede_acceder = True
                elif tipo_ticket_asociado == "municipio" and \
                     current_user.rubro and current_user.rubro.nombre.lower().strip() == "municipios" and \
                     hasattr(current_user, "municipio_id") and ticket_asociado.municipio_id == current_user.municipio_id: # Admin/empleado del municipio
                    puede_acceder = True
                elif tipo_ticket_asociado == "pyme" and \
                     current_user.rubro_id and ticket_asociado.rubro_id == current_user.rubro_id: # Admin/empleado de la pyme
                    puede_acceder = True
    
    if not puede_acceder:
        return jsonify({"error": "No tienes permiso para acceder a este archivo."}), 403

    file_path = os.path.join(TICKET_ATTACHMENT_FOLDER, safe_filename)
    if not os.path.exists(file_path):
        current_app.logger.error(f"El archivo {safe_filename} no existe en el filesystem aunque sí en DB.")
        return jsonify({"error": "Archivo no encontrado en el servidor."}), 404
    
    return send_from_directory(TICKET_ATTACHMENT_FOLDER, safe_filename, as_attachment=False) # as_attachment=True para forzar descarga
