from flask import Blueprint, jsonify, request, current_app
from models import (
    User,
    Conversacion,
    PymeTicket,
    MunicipioTicket,
    TicketComentario,
    ArchivoAdjunto,
)
from extensions import db
from routes.auth import token_requerido, admin_o_empleado_requerido
from sqlalchemy import or_

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')


def _obtener_clientes(
    current_user: User,
    tag: str | None = None,
    q: str | None = None,
    acepta_marketing: str | None = None,
    sort: str | None = None,
    order: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
):
    """Obtiene los clientes permitiendo búsqueda y filtros opcionales.

    Args:
        current_user: Usuario dueño de los clientes.
        tag: Filtrar por tag existente.
        q: Término de búsqueda en nombre, email o teléfono.
        acepta_marketing: 'true' / 'false' para filtrar por suscripción.
        sort: Campo por el cual ordenar (name, email, telefono).
        order: 'asc' o 'desc'.
        limit: Cantidad máxima de registros a devolver.
        offset: Desplazamiento inicial de los resultados.
    """
    query = User.query.filter_by(empresa_id=current_user.id)
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
    return [
        {
            "id": c.id,
            "name": c.name,
            "email": c.email,
            "telefono": c.telefono,
            "acepta_marketing": c.acepta_marketing,
            "latitud": c.latitud,
            "longitud": c.longitud,
            "tags": c.tags.split(',') if c.tags else [],
        }
        for c in clientes
    ]

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
    resultado = _obtener_clientes(
        current_user,
        tag,
        q=q,
        acepta_marketing=marketing,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return jsonify(resultado)


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
    resultado = _obtener_clientes(
        current_user,
        tag,
        q=q,
        acepta_marketing=marketing,
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return jsonify(resultado)


@crm_bp.route('/clientes/<int:cliente_id>/tags', methods=['PUT'])
@token_requerido
@admin_o_empleado_requerido
def actualizar_tags(current_user: User, cliente_id: int):
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
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

    total = User.query.filter_by(empresa_id=current_user.id).count()
    marketing = User.query.filter_by(empresa_id=current_user.id, acepta_marketing=True).count()

    abiertos_muni = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM municipio_ticket mt JOIN user u ON mt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND mt.estado != 'cerrado'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0

    abiertos_pyme = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM pyme_ticket pt JOIN user u ON pt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND pt.estado != 'cerrado'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0

    cerrados_muni = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM municipio_ticket mt JOIN user u ON mt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND mt.estado = 'cerrado'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0

    cerrados_pyme = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM pyme_ticket pt JOIN user u ON pt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND pt.estado = 'cerrado'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0
    return jsonify({
        "total_clientes": total,
        "aceptan_marketing": marketing,
        "tickets_abiertos": abiertos_muni + abiertos_pyme,
        "tickets_cerrados": cerrados_muni + cerrados_pyme,
    })


def _obtener_interacciones(cliente: User):
    """Compila el historial de chats y tickets de un cliente."""
    chats = Conversacion.query.filter_by(user_id=cliente.id).all()
    pymes = PymeTicket.query.filter_by(user_id=cliente.id).all()
    munis = MunicipioTicket.query.filter_by(user_id=cliente.id).all()
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
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    historial = _obtener_interacciones(cliente)
    return jsonify(historial)


@crm_bp.route('/campanas/enviar', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def enviar_campana(current_user: User):
    """Mock de envío de campañas masivas."""
    data = request.get_json(silent=True) or {}
    mensaje = data.get('mensaje')
    usuarios = data.get('usuarios', [])
    if not mensaje or not isinstance(usuarios, list):
        return jsonify({"error": "Datos inválidos"}), 400

    clientes = User.query.filter(User.id.in_(usuarios), User.empresa_id == current_user.id).all()
    for cli in clientes:
        current_app.logger.info(f"[CRM] Enviar campaña a {cli.email}: {mensaje}")
    return jsonify({"enviados": len(clientes)})


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


def _obtener_historial_cliente(cliente_id: int) -> dict:
    """Compila interacciones previas del cliente con mayor detalle."""
    convs = (
        Conversacion.query.filter_by(user_id=cliente_id)
        .order_by(Conversacion.timestamp.desc())
        .all()
    )
    tickets_pyme = (
        PymeTicket.query.filter_by(user_id=cliente_id)
        .order_by(PymeTicket.fecha.desc())
        .all()
    )
    tickets_muni = (
        MunicipioTicket.query.filter_by(user_id=cliente_id)
        .order_by(MunicipioTicket.fecha.desc())
        .all()
    )

    consultas = []
    tickets = []
    archivos = []
    timeline = []

    adjuntos_chat = (
        ArchivoAdjunto.query.filter_by(user_id=cliente_id, tipo="chat")
        .order_by(ArchivoAdjunto.fecha.desc())
        .all()
    )

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

    timeline.sort(key=lambda x: x["fecha"], reverse=True)

    return {
        "consultas": consultas,
        "tickets": tickets,
        "archivos": archivos,
        "timeline": timeline,
    }


@crm_bp.route('/clientes/<int:cliente_id>/historial', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def historial_cliente(current_user: User, cliente_id: int):
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    datos = _obtener_historial_cliente(cliente.id)
    return jsonify(datos)

