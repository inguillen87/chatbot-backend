from flask import Blueprint, jsonify, request, current_app
from models import User
from extensions import db
from routes.auth import token_requerido

crm_bp = Blueprint('crm', __name__, url_prefix='/crm')


def _obtener_clientes(current_user: User, tag: str | None = None):
    query = User.query.filter_by(empresa_id=current_user.id)
    if tag:
        like = f"%{tag}%"
        query = query.filter(User.tags.ilike(like))
    clientes = query.order_by(User.name.asc()).all()
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
def listar_clientes(current_user: User):
    """Devuelve los usuarios asociados a la empresa o municipio del token."""
    if current_user.empresa_id is not None:
        return jsonify([])
    tag = request.args.get('tag')
    resultado = _obtener_clientes(current_user, tag)
    return jsonify(resultado)


@crm_bp.route('/usuarios', methods=['GET'])
@token_requerido
def listar_usuarios(current_user: User):
    """Alias de /clientes por compatibilidad."""
    if current_user.empresa_id is not None:
        return jsonify([])
    tag = request.args.get('tag')
    resultado = _obtener_clientes(current_user, tag)
    return jsonify(resultado)


@crm_bp.route('/clientes/<int:cliente_id>/tags', methods=['PUT'])
@token_requerido
def actualizar_tags(current_user: User, cliente_id: int):
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
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
def analytics(current_user: User):
    """Devuelve métricas básicas de usuarios y tickets."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403

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
def historial_cliente(current_user: User, cliente_id: int):
    """Devuelve consultas previas y tickets de un cliente."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    cliente = User.query.filter_by(id=cliente_id, empresa_id=current_user.id).first()
    if not cliente:
        return jsonify({"error": "Cliente no encontrado"}), 404
    historial = _obtener_interacciones(cliente)
    return jsonify(historial)


@crm_bp.route('/campanas/enviar', methods=['POST'])
@token_requerido
def enviar_campana(current_user: User):
    """Mock de envío de campañas masivas."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403
    data = request.get_json(silent=True) or {}
    mensaje = data.get('mensaje')
    usuarios = data.get('usuarios', [])
    if not mensaje or not isinstance(usuarios, list):
        return jsonify({"error": "Datos inválidos"}), 400

    clientes = User.query.filter(User.id.in_(usuarios), User.empresa_id == current_user.id).all()
    for cli in clientes:
        current_app.logger.info(f"[CRM] Enviar campaña a {cli.email}: {mensaje}")
    return jsonify({"enviados": len(clientes)})
