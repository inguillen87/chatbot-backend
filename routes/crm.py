from flask import Blueprint, jsonify, request, current_app
from models import (
    User,
    Conversacion,
    PymeTicket,
    MunicipioTicket,
    TicketComentario,
    ArchivoAdjunto,
    ClienteNota, # Nueva importación
)
from extensions import db
from sqlalchemy import or_
from routes.auth import token_requerido, admin_o_empleado_requerido
from sqlalchemy import or_
from datetime import datetime, timedelta # Añadido timedelta

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
    en_proceso_muni = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM municipio_ticket mt JOIN user u ON mt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND mt.estado = 'en_proceso'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0

    en_proceso_pyme = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM pyme_ticket pt JOIN user u ON pt.user_id = u.id "
            "WHERE u.empresa_id = :eid AND pt.estado = 'en_proceso'"
        ),
        {"eid": current_user.id},
    ).scalar() or 0

    tasa_conversion_marketing = (marketing / total) * 100 if total > 0 else 0

    # Trend calculation for new clients (last 30 days vs previous 30 days)
    hoy = datetime.utcnow()
    inicio_periodo_actual = hoy - timedelta(days=30)
    fin_periodo_anterior = inicio_periodo_actual
    inicio_periodo_anterior = fin_periodo_anterior - timedelta(days=30)

    nuevos_clientes_actual = User.query.filter(
        User.empresa_id == current_user.id,
        User.fecha_creacion >= inicio_periodo_actual
    ).count() # Asume que fecha_creacion no puede ser en el futuro

    nuevos_clientes_anterior = User.query.filter(
        User.empresa_id == current_user.id,
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
    asunto = data.get('asunto')
    mensaje_html = data.get('mensaje_html')
    mensaje_texto = data.get('mensaje_texto', '') # Opcional
    lista_ids_usuarios = data.get('usuarios', []) # Lista de user_ids de clientes

    if not asunto or not mensaje_html or not isinstance(lista_ids_usuarios, list) or not lista_ids_usuarios:
        return jsonify({"error": "Datos inválidos. Se requiere 'asunto', 'mensaje_html' y una lista de 'usuarios'."}), 400

    # Validar que los usuarios pertenezcan a la empresa del current_user
    clientes_destinatarios = User.query.filter(
        User.id.in_(lista_ids_usuarios),
        User.empresa_id == current_user.id,
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

    # Obtener y añadir notas del cliente
    notas_cliente = (
        ClienteNota.query.filter_by(cliente_user_id=cliente_id)
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
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    datos = _obtener_historial_cliente(cliente.id)
    return jsonify(datos)


# --- Rutas para Notas de Clientes ---

def _serialize_nota(nota: ClienteNota, creador_email: str = "N/A"):
    return {
        "id": nota.id,
        "cliente_user_id": nota.cliente_user_id,
        "creada_por_user_id": nota.creada_por_user_id,
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
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado o no pertenece a esta empresa."}), 404

    data = request.get_json()
    if not data or not data.get('nota'):
        return jsonify({"error": "El contenido de la nota es obligatorio."}), 400

    nueva_nota = ClienteNota(
        cliente_user_id=cliente_id,
        creada_por_user_id=current_user.id, # El admin/empleado actual es el creador
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
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado o no pertenece a esta empresa."}), 404

    notas = ClienteNota.query.filter_by(cliente_user_id=cliente_id)\
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
    nota = ClienteNota.query.get(nota_id)
    if not nota:
        return jsonify({"error": "Nota no encontrada."}), 404

    # Verificar que el cliente de la nota pertenezca a la empresa del current_user (admin/empleado)
    cliente_de_nota = User.query.get(nota.cliente_user_id)
    if not cliente_de_nota or cliente_de_nota.empresa_id != current_user.id:
        return jsonify({"error": "No tiene permiso para modificar esta nota (cliente no asociado)."}), 403

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
    nota = ClienteNota.query.get(nota_id)
    if not nota:
        return jsonify({"error": "Nota no encontrada."}), 404

    cliente_de_nota = User.query.get(nota.cliente_user_id)
    if not cliente_de_nota or cliente_de_nota.empresa_id != current_user.id:
        return jsonify({"error": "No tiene permiso para eliminar esta nota (cliente no asociado)."}), 403

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
    clients = User.query.filter_by(empresa_id=current_user.id).all()
    recent_clients_data = []

    for client in clients:
        last_interaction_date = None

        # Check Conversaciones
        last_convo = Conversacion.query.filter_by(user_id=client.id).order_by(Conversacion.timestamp.desc()).first()
        if last_convo:
            # Asegurar que last_convo.timestamp es offset-naive si se compara con datetime.utcnow()
            # Asumiendo que todos los timestamps son UTC.
            if not last_interaction_date or last_convo.timestamp > last_interaction_date:
                last_interaction_date = last_convo.timestamp

        # Check PymeTickets (usar ultima_actividad que se actualiza)
        last_pyme_ticket = PymeTicket.query.filter_by(user_id=client.id).order_by(PymeTicket.ultima_actividad.desc()).first()
        if last_pyme_ticket:
            if not last_interaction_date or last_pyme_ticket.ultima_actividad > last_interaction_date:
                last_interaction_date = last_pyme_ticket.ultima_actividad

        # Check MunicipioTickets (usar ultima_actividad)
        last_muni_ticket = MunicipioTicket.query.filter_by(user_id=client.id).order_by(MunicipioTicket.ultima_actividad.desc()).first()
        if last_muni_ticket:
            if not last_interaction_date or last_muni_ticket.ultima_actividad > last_interaction_date:
                last_interaction_date = last_muni_ticket.ultima_actividad

        # Check ClienteNota (usar fecha_actualizacion)
        last_nota = ClienteNota.query.filter_by(cliente_user_id=client.id).order_by(ClienteNota.fecha_actualizacion.desc()).first()
        if last_nota:
            if not last_interaction_date or last_nota.fecha_actualizacion > last_interaction_date:
                last_interaction_date = last_nota.fecha_actualizacion

        if last_interaction_date and last_interaction_date >= cutoff_date:
            recent_clients_data.append({
                "id": client.id, "name": client.name, "email": client.email,
                "telefono": client.telefono, # Añadir teléfono para rápida visualización/acción
                "last_interaction_date": last_interaction_date.isoformat(),
                "tags": client.tags.split(',') if client.tags else []
            })

    return jsonify(sorted(recent_clients_data, key=lambda x: x["last_interaction_date"], reverse=True))

@crm_bp.route('/clientes/insights/needs_followup', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def get_needs_followup_clients(current_user: User):
    days_threshold = request.args.get('days', 30, type=int)
    # Clients whose last interaction was BEFORE this cutoff date, or never interacted
    cutoff_date = datetime.utcnow() - timedelta(days=days_threshold)

    clients = User.query.filter_by(empresa_id=current_user.id).all()
    needs_followup_clients_data = []

    for client in clients:
        last_interaction_date = None

        last_convo = Conversacion.query.filter_by(user_id=client.id).order_by(Conversacion.timestamp.desc()).first()
        if last_convo:
            if not last_interaction_date or last_convo.timestamp > last_interaction_date:
                last_interaction_date = last_convo.timestamp

        last_pyme_ticket = PymeTicket.query.filter_by(user_id=client.id).order_by(PymeTicket.ultima_actividad.desc()).first()
        if last_pyme_ticket:
            if not last_interaction_date or last_pyme_ticket.ultima_actividad > last_interaction_date:
                last_interaction_date = last_pyme_ticket.ultima_actividad

        last_muni_ticket = MunicipioTicket.query.filter_by(user_id=client.id).order_by(MunicipioTicket.ultima_actividad.desc()).first()
        if last_muni_ticket:
            if not last_interaction_date or last_muni_ticket.ultima_actividad > last_interaction_date:
                last_interaction_date = last_muni_ticket.ultima_actividad

        last_nota = ClienteNota.query.filter_by(cliente_user_id=client.id).order_by(ClienteNota.fecha_actualizacion.desc()).first()
        if last_nota:
            if not last_interaction_date or last_nota.fecha_actualizacion > last_interaction_date:
                last_interaction_date = last_nota.fecha_actualizacion

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
