from flask import Blueprint, jsonify, request, current_app, render_template
import re
from models import (
    User,
    Conversacion,
    PymeTicket,
    MunicipioTicket,
    TicketComentario,
    ArchivoAdjunto,
    ChatSessionContext,
    ClienteNota, # Nueva importación
    LlmInteractionLog,
    TenantProfile,
)
from extensions import db
from sqlalchemy import and_, false, or_
from utils.auth_helpers import (
    admin_o_empleado_requerido,
    auth_tenant_for_user,
    token_requerido,
)
from datetime import datetime, timedelta # Añadido timedelta

from services.employee_ticket_access import apply_employee_ticket_category_scope
from services.tenant_ticket_scope import (
    scoped_municipio_ticket_query,
    tenant_unique_legacy_owner_id,
)
from utils.roles import is_authorized_superadmin_user


crm_bp = Blueprint('crm', __name__, url_prefix='/crm')


def _crm_tenant_for_actor(actor: User) -> TenantProfile | None:
    """Resolve one exact CRM tenant, never an arbitrary legacy owner match."""
    tenant = auth_tenant_for_user(actor)
    if tenant is None or getattr(tenant, "is_active", True) is False:
        return None
    return tenant


def _crm_pyme_ticket_query(actor: User, *, cliente_id: int | None = None):
    query = PymeTicket.query
    if cliente_id is not None:
        query = query.filter(PymeTicket.user_id == cliente_id)
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    if tenant is None:
        return query.filter(false())
    query = query.filter(PymeTicket.tenant_id == tenant.id)
    return apply_employee_ticket_category_scope(query, actor, PymeTicket)


def _crm_municipio_ticket_query(actor: User, *, cliente_id: int | None = None):
    query = MunicipioTicket.query
    if cliente_id is not None:
        query = query.filter(MunicipioTicket.user_id == cliente_id)
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    if tenant is None:
        return query.filter(false())
    query = scoped_municipio_ticket_query(tenant, query)
    return apply_employee_ticket_category_scope(query, actor, MunicipioTicket)


def _crm_conversation_query(actor: User, *, cliente_id: int):
    query = Conversacion.query.filter(Conversacion.user_id == cliente_id)
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    if tenant is None or tenant.pyme_id is None:
        return query.filter(false())
    return query.filter(Conversacion.pyme_id == tenant.pyme_id)


def _crm_client_query(actor: User):
    query = User.query
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    legacy_owner_id = tenant_unique_legacy_owner_id(tenant)
    if tenant is None:
        return query.filter(false())
    filters = [User.tenant_id == tenant.id]
    if legacy_owner_id is not None:
        filters.append(
            and_(
                User.tenant_id.is_(None),
                User.empresa_id == legacy_owner_id,
            )
        )
    return query.filter(or_(*filters))


def _crm_note_query(actor: User, *, cliente_id: int | None = None):
    query = ClienteNota.query
    if cliente_id is not None:
        query = query.filter(ClienteNota.cliente_user_id == cliente_id)
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    if tenant is None:
        return query.filter(false())
    # ``tenant_id`` is the historical authorization scope.  Deriving access
    # from the creator's *current* membership would make notes move between
    # organizations when an employee changes tenant.
    return query.filter(ClienteNota.tenant_id == tenant.id)


def _crm_note_write_tenant(actor: User, cliente: User) -> TenantProfile | None:
    if not is_authorized_superadmin_user(actor):
        return _crm_tenant_for_actor(actor)
    tenant_id = getattr(cliente, "tenant_id", None)
    if not tenant_id:
        return None
    tenant = db.session.get(TenantProfile, tenant_id)
    if tenant is None or getattr(tenant, "is_active", True) is False:
        return None
    return tenant


def _crm_llm_log_query(actor: User):
    """Return review rows from the actor's immutable tenant snapshot only."""

    query = LlmInteractionLog.query.filter_by(status="pending_review")
    if is_authorized_superadmin_user(actor):
        return query
    tenant = _crm_tenant_for_actor(actor)
    if tenant is None:
        return query.filter(false())
    return query.filter(LlmInteractionLog.tenant_id == tenant.id)


def _crm_last_interaction_date(actor: User, cliente_id: int):
    candidates = []
    last_convo = _crm_conversation_query(actor, cliente_id=cliente_id).order_by(
        Conversacion.timestamp.desc()
    ).first()
    if last_convo and last_convo.timestamp:
        candidates.append(last_convo.timestamp)

    last_pyme = _crm_pyme_ticket_query(actor, cliente_id=cliente_id).order_by(
        PymeTicket.ultima_actividad.desc()
    ).first()
    if last_pyme and last_pyme.ultima_actividad:
        candidates.append(last_pyme.ultima_actividad)

    last_municipio = _crm_municipio_ticket_query(actor, cliente_id=cliente_id).order_by(
        MunicipioTicket.ultima_actividad.desc()
    ).first()
    if last_municipio and last_municipio.ultima_actividad:
        candidates.append(last_municipio.ultima_actividad)

    last_note = _crm_note_query(actor, cliente_id=cliente_id).order_by(
        ClienteNota.fecha_actualizacion.desc()
    ).first()
    if last_note and last_note.fecha_actualizacion:
        candidates.append(last_note.fecha_actualizacion)
    return max(candidates) if candidates else None

@crm_bp.route('/')
@token_requerido
@admin_o_empleado_requerido
def index(current_user: User):
    return """
    <html>
        <head><title>CRM</title></head>
        <body>
            <h1>CRM Dashboard</h1>
            <p><a href="/tickets/panel">Ver Panel de Tickets</a></p>
        </body>
    </html>
    """

def _obtener_clientes(
    current_user: User,
    tag: str | None = None,
    q: str | None = None,
    acepta_marketing: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    page: int | None = None,
    page_size: int | None = None,
):
    """Obtiene los clientes permitiendo búsqueda, filtros y paginación opcional.

    Args:
        current_user: Usuario dueño de los clientes.
        tag: Filtrar por tag existente.
        q: Término de búsqueda en nombre, email o teléfono.
        acepta_marketing: 'true' / 'false' para filtrar por suscripción.
        sort: Campo por el cual ordenar (name, email, telefono).
        order: 'asc' o 'desc'.
        limit: Cantidad máxima de registros a devolver (modo compatibilidad).
        offset: Desplazamiento inicial de los resultados (modo compatibilidad).
        page: Número de página (1-indexado) para paginación.
        page_size: Cantidad de registros por página.
    """
    query = _crm_client_query(current_user)
    if tag:
        like = f"%{tag}%"
        query = query.filter(User.tags.ilike(like))
    if q:
        like = f"%{q}%"
        query = query.filter(
            or_(User.name.ilike(like), User.email.ilike(like), User.telefono.ilike(like))
        )
    if acepta_marketing is not None:
        val = acepta_marketing.lower() in {"1", "true", "t", "yes", "si"}
        query = query.filter(User.acepta_marketing == val)
    if sort not in {"name", "email", "telefono", "id"}:
        sort = "name"
    columna = getattr(User, sort)
    if order == "desc":
        query = query.order_by(columna.desc())
    else:
        query = query.order_by(columna.asc())

    usar_paginacion = page_size is not None or page is not None
    if usar_paginacion:
        try:
            page_val = max(int(page or 1), 1)
        except (TypeError, ValueError):
            page_val = 1
        try:
            page_size_val = min(max(int(page_size or 50), 1), 500)
        except (TypeError, ValueError):
            page_size_val = 50
        total = query.order_by(None).count()
        query = query.limit(page_size_val).offset((page_val - 1) * page_size_val)
    else:
        if offset is not None:
            try:
                offset_val = int(offset)
                if offset_val >= 0:
                    query = query.offset(offset_val)
            except (TypeError, ValueError):
                pass
        if limit is not None:
            try:
                limit_val = int(limit)
                if limit_val >= 0:
                    query = query.limit(limit_val)
            except (TypeError, ValueError):
                pass

    clientes = query.all()
    data = [
        {
            "id": c.id,
            # Normalizamos strings para evitar valores None que rompan el front al aplicar toLowerCase
            "name": c.name or "",
            "email": c.email or "",
            "telefono": c.telefono or "",
            "acepta_marketing": c.acepta_marketing,
            "latitud": c.latitud,
            "longitud": c.longitud,
            "tags": c.tags.split(',') if c.tags else [],
        }
        for c in clientes
    ]

    if usar_paginacion:
        return {
            "items": data,
            "total": total,
            "page": page_val,
            "page_size": page_size_val,
            "pages": (total + page_size_val - 1) // page_size_val if total else 0,
        }

    return data

@crm_bp.route('/clientes', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def listar_clientes(current_user: User):
    """Devuelve los usuarios asociados a la empresa o municipio del token."""
    tag = request.args.get('tag')
    q = request.args.get('q')
    marketing = request.args.get('acepta_marketing')
    sort = request.args.get('sort')
    order = request.args.get('order')
    limit = request.args.get('limit')
    offset = request.args.get('offset')
    page = request.args.get('page')
    page_size = request.args.get('page_size')
    resultado = _obtener_clientes(
        current_user,
        tag,
        q=q,
        acepta_marketing=marketing,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        page=page,
        page_size=page_size,
    )
    return jsonify(resultado)


@crm_bp.route('/chat/location', methods=['POST'])
@token_requerido
def update_location(current_user: User):
    """Actualiza la ubicación de un usuario."""
    data = request.get_json()
    if not data or 'latitud' not in data or 'longitud' not in data:
        return jsonify({"error": "Datos de ubicación inválidos."}), 400

    user = User.query.get(current_user.id)
    if not user:
        return jsonify({"error": "Usuario no encontrado."}), 404

    user.latitud = data['latitud']
    user.longitud = data['longitud']
    try:
        db.session.commit()
        return jsonify({"mensaje": "Ubicación actualizada correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar ubicación para usuario {current_user.id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar la ubicación."}), 500


@crm_bp.route('/usuarios', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def listar_usuarios(current_user: User):
    """Alias de /clientes por compatibilidad."""
    tag = request.args.get('tag')
    q = request.args.get('q')
    marketing = request.args.get('acepta_marketing')
    sort = request.args.get('sort')
    order = request.args.get('order')
    limit = request.args.get('limit')
    offset = request.args.get('offset')
    page = request.args.get('page')
    page_size = request.args.get('page_size')
    resultado = _obtener_clientes(
        current_user,
        tag,
        q=q,
        acepta_marketing=marketing,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
        page=page,
        page_size=page_size,
    )
    return jsonify(resultado)


@crm_bp.route('/usuarios/<int:usuario_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_usuario(current_user: User, usuario_id: int):
    """Permite actualizar datos básicos y el rol de un usuario del tenant."""

    payload = request.get_json(silent=True) or {}
    usuario = _crm_client_query(current_user).filter(User.id == usuario_id).one_or_none()
    if not usuario:
        return jsonify({"error": "Usuario no encontrado"}), 404

    nuevo_nombre = payload.get("name")
    nuevo_email = payload.get("email")
    nuevo_rol = payload.get("rol")

    if nuevo_nombre is not None:
        if not isinstance(nuevo_nombre, str) or not nuevo_nombre.strip():
            return jsonify({"error": "El nombre no puede estar vacío."}), 400
        usuario.name = nuevo_nombre.strip()

    if nuevo_email is not None:
        if not isinstance(nuevo_email, str) or not nuevo_email.strip():
            return jsonify({"error": "El email es obligatorio."}), 400
        patron_email = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
        if not re.match(patron_email, nuevo_email.strip()):
            return jsonify({"error": "Formato de email inválido."}), 400
        correo_final = nuevo_email.strip().lower()
        existe = User.query.filter(User.email == correo_final, User.id != usuario.id).first()
        if existe:
            return jsonify({"error": "El email ya está en uso."}), 400
        usuario.email = correo_final

    if nuevo_rol is not None:
        roles_permitidos = {"usuario", "operador", "admin"}
        if nuevo_rol not in roles_permitidos:
            return jsonify({"error": "Rol inválido."}), 400
        usuario.rol = nuevo_rol

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "No se pudo actualizar el usuario."}), 500

    return jsonify(
        {
            "id": usuario.id,
            "name": usuario.name,
            "email": usuario.email,
            "rol": usuario.rol,
        }
    )


@crm_bp.route('/clientes/<int:cliente_id>/tags', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_tags(current_user: User, cliente_id: int):
    cliente = _crm_client_query(current_user).filter(User.id == cliente_id).one_or_none()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    tags = data.get('tags', [])
    if not isinstance(tags, list):
        return jsonify({"error": "'tags' debe ser una lista"}), 400
    cliente.tags = ','.join(tags)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al actualizar"}), 500
    return jsonify({"id": cliente.id, "tags": tags})


@crm_bp.route('/analytics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def analytics(current_user: User):
    """Devuelve métricas básicas de usuarios y tickets."""

    client_query = _crm_client_query(current_user)
    total = client_query.count()
    marketing = client_query.filter(User.acepta_marketing.is_(True)).count()

    client_ids = client_query.with_entities(User.id)
    muni_tickets = _crm_municipio_ticket_query(current_user).filter(
        MunicipioTicket.user_id.in_(client_ids)
    )
    pyme_tickets = _crm_pyme_ticket_query(current_user).filter(
        PymeTicket.user_id.in_(client_ids)
    )

    abiertos_muni = muni_tickets.filter(MunicipioTicket.estado != 'cerrado').count()
    abiertos_pyme = pyme_tickets.filter(PymeTicket.estado != 'cerrado').count()
    cerrados_muni = muni_tickets.filter(MunicipioTicket.estado == 'cerrado').count()
    cerrados_pyme = pyme_tickets.filter(PymeTicket.estado == 'cerrado').count()
    en_proceso_muni = muni_tickets.filter(MunicipioTicket.estado == 'en_proceso').count()
    en_proceso_pyme = pyme_tickets.filter(PymeTicket.estado == 'en_proceso').count()

    tasa_conversion_marketing = (marketing / total) * 100 if total > 0 else 0

    # Trend calculation for new clients (last 30 days vs previous 30 days)
    hoy = datetime.utcnow()
    inicio_periodo_actual = hoy - timedelta(days=30)
    fin_periodo_anterior = inicio_periodo_actual
    inicio_periodo_anterior = fin_periodo_anterior - timedelta(days=30)

    nuevos_clientes_actual = _crm_client_query(current_user).filter(
        User.fecha_creacion >= inicio_periodo_actual
    ).count() # Asume que fecha_creacion no puede ser en el futuro

    nuevos_clientes_anterior = _crm_client_query(current_user).filter(
        User.fecha_creacion >= inicio_periodo_anterior,
        User.fecha_creacion < fin_periodo_anterior
    ).count()

    tendencia_nuevos_clientes = 0
    if nuevos_clientes_anterior > 0:
        tendencia_nuevos_clientes = round(((nuevos_clientes_actual - nuevos_clientes_anterior) / nuevos_clientes_anterior) * 100, 2)
    elif nuevos_clientes_actual > 0: # Si antes era 0 y ahora hay, es un aumento "infinito"
        tendencia_nuevos_clientes = 100.0 # O un valor especial, o simplemente mostrar los números

    return jsonify({
        "total_clientes": total,
        "aceptan_marketing": marketing,
        "tasa_conversion_marketing_percent": round(tasa_conversion_marketing, 2),
        "tickets_abiertos": abiertos_muni + abiertos_pyme,
        "tickets_en_proceso": en_proceso_muni + en_proceso_pyme,
        "tickets_cerrados": cerrados_muni + cerrados_pyme,
        "insights_periodo_dias": 30, # Informar el periodo usado para los insights
        "nuevos_clientes_periodo_actual": nuevos_clientes_actual,
        "nuevos_clientes_periodo_anterior": nuevos_clientes_anterior,
        "nuevos_clientes_tendencia_percent": tendencia_nuevos_clientes,
    })


def _obtener_interacciones(cliente: User, viewer_user: User):
    """Compila el historial de chats y tickets de un cliente, filtrado por el tenant del viewer."""

    # Filtrar conversaciones
    chats_query = _crm_conversation_query(viewer_user, cliente_id=cliente.id)
    # Para municipio, Conversacion no tiene municipio_id directo siempre?
    # Conversacion tiene user_id (cliente) y pyme_id.
    # Si es municipio, Conversacion podría no estar linkeada directamente por ID, o usa lógica distinta.
    # Asumimos que el CRM de municipio ve lo que le corresponde.
    # Si Conversacion no tiene municipio_id, es difícil filtrar.
    # Pero el modelo tiene 'pyme_id'.

    chats = chats_query.all()

    # Filtrar PymeTickets
    pymes_query = _crm_pyme_ticket_query(viewer_user, cliente_id=cliente.id)

    pymes = pymes_query.all()

    # Filtrar MunicipioTickets
    munis_query = _crm_municipio_ticket_query(viewer_user, cliente_id=cliente.id)

    munis = munis_query.all()

    historial = []
    for c in chats:
        historial.append({
            "tipo": "chat",
            "pregunta": c.pregunta,
            "respuesta": c.respuesta,
            "fecha": c.timestamp.isoformat(),
        })
    for t in pymes:
        historial.append({
            "tipo": "ticket_pyme",
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, "asunto", "N/A"),
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo": getattr(t, "archivo_url", None),
        })
    for t in munis:
        historial.append({
            "tipo": "ticket_municipio",
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, "asunto", "N/A"),
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo": getattr(t, "archivo_url", None),
        })
    historial.sort(key=lambda x: x["fecha"], reverse=True)
    return historial


@crm_bp.route('/clientes/<int:cliente_id>/interacciones', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def interacciones_cliente(current_user: User, cliente_id: int):
    """Devuelve consultas previas y tickets de un cliente."""
    cliente = _crm_client_query(current_user).filter(User.id == cliente_id).one_or_none()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    historial = _obtener_interacciones(cliente, current_user)
    return jsonify(historial)


@crm_bp.route('/campanas/enviar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def enviar_campana(current_user: User):
    """Mock de envío de campañas masivas."""
    data = request.get_json(silent=True) or {}
    asunto = data.get('asunto')
    mensaje_html = data.get('mensaje_html')
    mensaje_texto = data.get('mensaje_texto', '') # Opcional
    lista_ids_usuarios = data.get('usuarios', []) # Lista de user_ids de clientes

    if not asunto or not mensaje_html or not isinstance(lista_ids_usuarios, list) or not lista_ids_usuarios:
        return jsonify({"error": "Datos inválidos. Se requiere 'asunto', 'mensaje_html' y una lista de 'usuarios'."}), 400

    # Validar que los usuarios pertenezcan a la empresa del current_user
    clientes_destinatarios = _crm_client_query(current_user).filter(
        User.id.in_(lista_ids_usuarios),
        User.email.isnot(None), # Solo usuarios con email
        User.acepta_marketing == True # Solo usuarios que aceptan marketing
    ).all()

    if not clientes_destinatarios:
        return jsonify({"error": "No se encontraron clientes válidos para esta campaña (deben tener email y aceptar marketing)."}), 400

    current_app.logger.info(f"[CRM_CAMPAIGN] Iniciando envío de campaña '{asunto}' para {len(clientes_destinatarios)} clientes de la empresa ID {current_user.id}.")

    # --- Implementación con Celery ---
    from services.tasks import tarea_enviar_campana_email # Importar la tarea Celery

    ids_clientes_finales = [cli.id for cli in clientes_destinatarios]

    # Encolar la tarea Celery
    tarea_enviar_campana_email.delay(
        empresa_id_solicitante=current_user.id, # Para logging y contexto en la tarea
        lista_ids_clientes_destinatarios=ids_clientes_finales,
        asunto=asunto,
        cuerpo_html=mensaje_html,
        cuerpo_texto=mensaje_texto
    )

    current_app.logger.info(f"[CRM_CAMPAIGN] Tarea Celery para enviar campaña '{asunto}' a {len(ids_clientes_finales)} clientes ha sido encolada.")

    return jsonify({
        "mensaje": f"Campaña '{asunto}' programada para envío a {len(ids_clientes_finales)} clientes. El proceso se realizará en segundo plano.",
        "clientes_potenciales": len(lista_ids_usuarios),
        "clientes_contactados_programados": len(ids_clientes_finales)
    }), 202 # 202 Accepted: la solicitud ha sido aceptada para procesamiento


def _detalles_archivo(adjunto: ArchivoAdjunto, relacion: dict) -> dict:
    """Devuelve metadatos simples del archivo."""
    return {
        "nombre": adjunto.nombre_original or adjunto.filename,
        "tipo": adjunto.mime,
        "tamano": adjunto.tamano,
        "fecha": adjunto.fecha.isoformat() if adjunto.fecha else None,
        "usuario_id": adjunto.user_id,
        "url": adjunto.url,
        "relacion": relacion,
    }


def _obtener_historial_cliente(cliente_id: int, viewer_user: User) -> dict:
    """Compila interacciones previas del cliente con mayor detalle."""
    convs = (
        _crm_conversation_query(viewer_user, cliente_id=cliente_id)
        .order_by(Conversacion.timestamp.desc())
        .all()
    )
    tickets_pyme = (
        _crm_pyme_ticket_query(viewer_user, cliente_id=cliente_id)
        .order_by(PymeTicket.fecha.desc())
        .all()
    )
    tickets_muni = (
        _crm_municipio_ticket_query(viewer_user, cliente_id=cliente_id)
        .order_by(MunicipioTicket.fecha.desc())
        .all()
    )

    consultas = []
    tickets = []
    archivos = []
    timeline = []

    chat_session_ids = [c.session_id for c in convs if c.session_id]
    adjuntos_chat_query = ArchivoAdjunto.query.filter_by(user_id=cliente_id, tipo="chat")
    if not is_authorized_superadmin_user(viewer_user):
        if chat_session_ids:
            adjuntos_chat_query = adjuntos_chat_query.filter(
                ArchivoAdjunto.session_id.in_(chat_session_ids)
            )
        else:
            adjuntos_chat_query = adjuntos_chat_query.filter(false())
    adjuntos_chat = adjuntos_chat_query.order_by(ArchivoAdjunto.fecha.desc()).all()

    for c in convs:
        consulta = {
            "id": c.id,
            "pregunta": c.pregunta,
            "respuesta": c.respuesta,
            "fecha": c.timestamp.isoformat(),
            "fuente": c.fuente,
            "rubro": c.rubro,
        }
        consultas.append(consulta)
        timeline.append({"tipo": "consulta", **consulta})

    def _comentarios(ticket, field_name):
        return [
            {
                "id": com.id,
                "texto": com.comentario,
                "fecha": com.fecha.isoformat(),
                "user_id": com.user_id,
                "es_admin": com.es_admin,
            }
            for com in ticket.comentarios.order_by(TicketComentario.fecha.asc()).all()
        ]

    for t in tickets_pyme:
        ticket_data = {
            "id": t.id,
            "tipo": "pyme",
            "nro_ticket": t.nro_ticket,
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo_url": getattr(t, "archivo_url", None),
            "mensajes": _comentarios(t, "pyme_ticket_id"),
        }
        tickets.append(ticket_data)
        timeline.append({"tipo": "ticket_pyme", **ticket_data})
        adjuntos_ticket = ArchivoAdjunto.query.filter_by(pyme_ticket_id=t.id).all()
        for a in adjuntos_ticket:
            meta = _detalles_archivo(a, {"tipo": "ticket_pyme", "id": t.id})
            archivos.append(meta)
            timeline.append({"tipo": "archivo_ticket_pyme", **meta})

    for t in tickets_muni:
        ticket_data = {
            "id": t.id,
            "tipo": "municipio",
            "nro_ticket": t.nro_ticket,
            "estado": t.estado,
            "fecha": t.fecha.isoformat(),
            "archivo_url": getattr(t, "archivo_url", None),
            "mensajes": _comentarios(t, "municipio_ticket_id"),
        }
        tickets.append(ticket_data)
        timeline.append({"tipo": "ticket_municipio", **ticket_data})
        adjuntos_ticket = ArchivoAdjunto.query.filter_by(municipio_ticket_id=t.id).all()
        for a in adjuntos_ticket:
            meta = _detalles_archivo(a, {"tipo": "ticket_municipio", "id": t.id})
            archivos.append(meta)
            timeline.append({"tipo": "archivo_ticket_municipio", **meta})

    for a in adjuntos_chat:
        meta = _detalles_archivo(a, {"tipo": "chat", "session_id": a.session_id})
        archivos.append(meta)
        timeline.append({"tipo": "archivo_chat", **meta})

    # Obtener y añadir notas del cliente
    notas_cliente = (
        _crm_note_query(viewer_user, cliente_id=cliente_id)
        .order_by(ClienteNota.fecha_creacion.desc()) # o fecha_actualizacion
        .all()
    )
    creador_ids_notas = list(set(n.creada_por_user_id for n in notas_cliente))
    creadores_notas_map = {c.id: c.email for c in User.query.filter(User.id.in_(creador_ids_notas)).all()}

    for nota_obj in notas_cliente:
        nota_data = {
            "id": nota_obj.id,
            "tipo": "nota_cliente",
            "texto": nota_obj.nota,
            "fecha": nota_obj.fecha_actualizacion.isoformat(), # Usar fecha_actualizacion para que ediciones recientes aparezcan
            "fecha_creacion": nota_obj.fecha_creacion.isoformat(),
            "creada_por_user_id": nota_obj.creada_por_user_id,
            "creador_email": creadores_notas_map.get(nota_obj.creada_por_user_id, "N/A")
        }
        # No añadir a 'consultas', 'tickets', o 'archivos' directamente, solo a la timeline.
        timeline.append(nota_data)

    timeline.sort(key=lambda x: x["fecha"], reverse=True)

    # El retorno original ya incluye la timeline.
    # Se podría considerar añadir un campo 'notas': [lista de notas serializadas] si se quiere acceder a ellas por separado además de en la timeline.
    # Por ahora, solo se añaden a la timeline.

    # Preparar una lista separada de notas serializadas para el retorno, además de la timeline
    notas_serializadas = []
    for nota_obj in notas_cliente: # Iterar de nuevo o almacenar la serialización antes
         notas_serializadas.append({
            "id": nota_obj.id,
            "texto": nota_obj.nota,
            "fecha_actualizacion": nota_obj.fecha_actualizacion.isoformat(),
            "fecha_creacion": nota_obj.fecha_creacion.isoformat(),
            "creada_por_user_id": nota_obj.creada_por_user_id,
            "creador_email": creadores_notas_map.get(nota_obj.creada_por_user_id, "N/A")
        })


    return {
        "consultas": consultas,
        "tickets": tickets,
        "archivos": archivos,
        "notas": notas_serializadas, # Lista dedicada de notas
        "timeline": timeline,
    }


@crm_bp.route('/clientes/<int:cliente_id>/historial', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def historial_cliente(current_user: User, cliente_id: int):
    cliente = _crm_client_query(current_user).filter(User.id == cliente_id).one_or_none()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    datos = _obtener_historial_cliente(cliente.id, current_user)
    return jsonify(datos)


# --- Rutas para Notas de Clientes ---

def _serialize_nota(nota: ClienteNota, creador_email: str = "N/A"):
    return {
        "id": nota.id,
        "cliente_user_id": nota.cliente_user_id,
        "creada_por_user_id": nota.creada_por_user_id,
        "tenant_id": nota.tenant_id,
        "creador_email": creador_email, # Email de quien creó la nota
        "nota": nota.nota,
        "fecha_creacion": nota.fecha_creacion.isoformat(),
        "fecha_actualizacion": nota.fecha_actualizacion.isoformat(),
    }

@crm_bp.route('/clientes/<int:cliente_id>/notas', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def crear_nota_cliente(current_user: User, cliente_id: int):
    """Crea una nueva nota para un cliente específico."""
    cliente = _crm_client_query(current_user).filter(User.id == cliente_id).one_or_none()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado o no pertenece a esta empresa."}), 404

    data = request.get_json()
    if not data or not data.get('nota'):
        return jsonify({"error": "El contenido de la nota es obligatorio."}), 400

    tenant = _crm_note_write_tenant(current_user, cliente)
    if tenant is None:
        return jsonify({"error": "No se pudo determinar el tenant de la nota."}), 409

    nueva_nota = ClienteNota(
        cliente_user_id=cliente_id,
        creada_por_user_id=current_user.id, # El admin/empleado actual es el creador
        tenant_id=tenant.id,
        nota=data['nota']
    )
    db.session.add(nueva_nota)
    try:
        db.session.commit()
        # Obtener el email del creador para la serialización
        creador = User.query.get(current_user.id)
        return jsonify(_serialize_nota(nueva_nota, creador.email if creador else "N/A")), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al crear nota para cliente {cliente_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al guardar la nota."}), 500

@crm_bp.route('/clientes/<int:cliente_id>/notas', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def listar_notas_cliente(current_user: User, cliente_id: int):
    """Lista todas las notas de un cliente específico."""
    cliente = _crm_client_query(current_user).filter(User.id == cliente_id).one_or_none()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado o no pertenece a esta empresa."}), 404

    notas = _crm_note_query(current_user, cliente_id=cliente_id)\
                             .order_by(ClienteNota.fecha_creacion.desc())\
                             .all()

    # Para obtener el email del creador de cada nota eficientemente
    creador_ids = list(set(n.creada_por_user_id for n in notas))
    creadores = User.query.filter(User.id.in_(creador_ids)).all()
    creadores_map = {c.id: c.email for c in creadores}

    return jsonify([_serialize_nota(n, creadores_map.get(n.creada_por_user_id, "N/A")) for n in notas])

@crm_bp.route('/notas/<int:nota_id>', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_nota_cliente(current_user: User, nota_id: int):
    """Actualiza una nota existente."""
    nota = _crm_note_query(current_user).filter(ClienteNota.id == nota_id).one_or_none()
    if not nota:
        return jsonify({"error": "Nota no encontrada."}), 404

    # Opcional: permitir solo al creador de la nota modificarla, o a cualquier admin/empleado de la empresa.
    # if nota.creada_por_user_id != current_user.id:
    #     return jsonify({"error": "No tiene permiso para modificar esta nota (no es el creador)."}), 403

    data = request.get_json()
    if not data or not data.get('nota'):
        return jsonify({"error": "El contenido de la nota es obligatorio."}), 400

    nota.nota = data['nota']
    # fecha_actualizacion se actualiza automáticamente por onupdate
    try:
        db.session.commit()
        creador = User.query.get(nota.creada_por_user_id)
        return jsonify(_serialize_nota(nota, creador.email if creador else "N/A"))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al actualizar nota {nota_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al actualizar la nota."}), 500

@crm_bp.route('/notas/<int:nota_id>', methods=['DELETE'])
@token_requerido
@admin_o_empleado_requerido
def eliminar_nota_cliente(current_user: User, nota_id: int):
    """Elimina una nota."""
    nota = _crm_note_query(current_user).filter(ClienteNota.id == nota_id).one_or_none()
    if not nota:
        return jsonify({"error": "Nota no encontrada."}), 404

    # Opcional: permitir solo al creador de la nota eliminarla.
    # if nota.creada_por_user_id != current_user.id:
    #    return jsonify({"error": "No tiene permiso para eliminar esta nota (no es el creador)."}), 403

    try:
        db.session.delete(nota)
        db.session.commit()
        return jsonify({"mensaje": "Nota eliminada correctamente."})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al eliminar nota {nota_id}: {e}", exc_info=True)
        return jsonify({"error": "Error interno al eliminar la nota."}), 500

# --- Rutas para Insights de Clientes ---

@crm_bp.route('/clientes/insights/recent', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_recent_clients(current_user: User):
    days_threshold = request.args.get('days', 7, type=int)
    cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)

    # Solo clientes de la empresa del admin/empleado actual
    clients = _crm_client_query(current_user).all()
    recent_clients_data = []

    for client in clients:
        last_interaction_date = _crm_last_interaction_date(current_user, client.id)

        if last_interaction_date and last_interaction_date >= cutoff_date:
            recent_clients_data.append({
                "id": client.id, "name": client.name, "email": client.email,
                "telefono": client.telefono, # Añadir teléfono para rápida visualización/acción
                "last_interaction_date": last_interaction_date.isoformat(),
                "tags": client.tags.split(',') if client.tags else []
            })

    return jsonify(sorted(recent_clients_data, key=lambda x: x["last_interaction_date"], reverse=True))

@crm_bp.route('/llm-review')
@token_requerido
@admin_o_empleado_requerido
def llm_review(current_user: User):
    """Muestra las interacciones del LLM que están pendientes de revisión."""
    logs = _crm_llm_log_query(current_user).order_by(
        LlmInteractionLog.created_at.desc()
    ).all()
    return render_template('admin/llm_review.html', logs=logs)

@crm_bp.route('/clientes/insights/needs_followup', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_needs_followup_clients(current_user: User):
    days_threshold = request.args.get('days', 30, type=int)
    # Clients whose last interaction was BEFORE this cutoff date, or never interacted
    cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)

    clients = _crm_client_query(current_user).all()
    needs_followup_clients_data = []

    for client in clients:
        last_interaction_date = _crm_last_interaction_date(current_user, client.id)

        if not last_interaction_date or last_interaction_date < cutoff_date:
            needs_followup_clients_data.append({
                "id": client.id, "name": client.name, "email": client.email,
                "telefono": client.telefono, # Añadir teléfono
                "last_interaction_date": last_interaction_date.isoformat() if last_interaction_date else None,
                "tags": client.tags.split(',') if client.tags else []
            })

    # Sort by last_interaction_date, putting None (never interacted) first or last based on preference
    # Here, None (never interacted) will come first when sorting ascending.
    return jsonify(sorted(needs_followup_clients_data, key=lambda x: (x["last_interaction_date"] is None, x["last_interaction_date"])))
