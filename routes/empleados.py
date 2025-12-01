from flask import Blueprint, request, jsonify
from models import User, TicketComentario, db, MunicipioTicket, PymeTicket
from routes.auth import token_requerido, solo_admin_requerido
from services.logic import es_rubro_publico
import uuid
from datetime import datetime, timedelta
from sqlalchemy import func
from routes.ticket import TICKET_ALLOWED_STATES
from services.categorias_municipio import CATEGORIAS_RECLAMO

def _normalize_categorias_input(categorias_raw):
    """Normaliza una lista de categorías proveniente del frontend.

    Acepta strings simples o diccionarios con las claves ``value`` / ``label``
    y devuelve una lista de nombres en minúsculas, sin duplicados ni valores
    vacíos.  No requiere que las categorías existan previamente en la base ya
    que se almacenan directamente en ``User.ticket_categorias`` para controlar
    el scope de cada empleado.
    """
    if not categorias_raw:
        return []

    nombres = []
    for item in categorias_raw:
        if isinstance(item, str):
            nombres.append(item)
        elif isinstance(item, dict):
            nombres.append(
                item.get("value")
                or item.get("label")
                or item.get("nombre")
                or item.get("name")
            )
        elif isinstance(item, int):
            # Permitimos IDs heredados de implementaciones anteriores, aunque
            # actualmente trabajamos solo con nombres.
            nombres.append(str(item))

    normalizadas = []
    for nombre in nombres:
        limpio = (nombre or "").strip().lower()
        if limpio:
            normalizadas.append(limpio)

    # Remover duplicados preservando orden
    return list(dict.fromkeys(normalizadas))


def _build_ticket_query_for_owner(current_user: User):
    """Return the base ticket query and model for the current owner/admin.

    This is used to compute per-employee load metrics while respecting the
    tenant boundaries (municipio vs pyme).
    """

    if current_user.tipo_chat == "municipio" and current_user.municipio_id:
        return (
            MunicipioTicket.query.filter(
                MunicipioTicket.municipio_id == current_user.municipio_id
            ),
            MunicipioTicket,
        )
    if current_user.tipo_chat == "pyme":
        tenant_pyme = getattr(current_user, "tenant_profile_pyme", None)
        if tenant_pyme:
            return (
                PymeTicket.query.filter(PymeTicket.tenant_id == tenant_pyme.id),
                PymeTicket,
            )
        elif current_user.rubro_id:
            return (
                PymeTicket.query.filter(PymeTicket.rubro_id == current_user.rubro_id),
                PymeTicket,
            )
    return None, None

empleados_bp = Blueprint('empleados', __name__, url_prefix='/empleados')

@empleados_bp.route('', methods=['GET'])
@token_requerido
@solo_admin_requerido
def listar_empleados(current_user: User):
    """Lista los empleados asociados al usuario actual."""
    empleados = (
        User.query.filter_by(empresa_id=current_user.id, rol='empleado')
        .order_by(User.name.asc())
        .all()
    )
    datos = []
    fecha_inicio_mes = datetime.utcnow() - timedelta(days=30)
    ticket_query_base, TicketModel = _build_ticket_query_for_owner(current_user)
    open_statuses = [estado for estado in TICKET_ALLOWED_STATES if estado != "cerrado"]

    for e in empleados:
        tickets_respondidos_mes = db.session.query(func.count(TicketComentario.id)).filter(
            TicketComentario.user_id == e.id,
            TicketComentario.es_admin == True, # Comentario hecho por un admin/empleado
            TicketComentario.fecha >= fecha_inicio_mes
        ).scalar() or 0

        categorias = e.ticket_categorias.split(",") if e.ticket_categorias else []
        normalized_categories = [c.strip().lower() for c in categorias if c.strip()]
        open_tickets = 0
        if ticket_query_base is not None and normalized_categories:
            open_tickets = (
                ticket_query_base.filter(
                    TicketModel.estado.in_(open_statuses),
                    func.lower(TicketModel.categoria).in_(normalized_categories),
                ).count()
            )

        datos.append({
            "key": e.id,
            "id": e.id,
            "name": e.name or "",
            "email": e.email or "",
            "rol": e.rol,
            # Evitamos valores None en la lista de categorías
            "categorias": [c for c in categorias if c],
            "tickets_respondidos_mes": tickets_respondidos_mes,
            "tickets_abiertos_categoria": open_tickets,
        })
    return jsonify(datos)


@empleados_bp.route('/categorias', methods=['GET'])
@token_requerido
@solo_admin_requerido
def obtener_categorias_empleado(current_user: User):
    """Devuelve la lista de categorías disponibles para asignar a empleados."""

    categorias_set = {c for c in CATEGORIAS_RECLAMO if c}

    # Agregar categorías dinámicas detectadas en los tickets existentes para el
    # tenant actual. Esto evita dejar al panel sin opciones cuando se cargaron
    # reclamos con nuevas etiquetas o cuando las categorías iniciales todavía
    # no se configuraron.
    try:
        if current_user.tipo_chat == "municipio" and current_user.municipio_id:
            categorias_en_bd = (
                db.session.query(MunicipioTicket.categoria)
                .filter(
                    MunicipioTicket.municipio_id == current_user.municipio_id,
                    MunicipioTicket.categoria.isnot(None),
                    MunicipioTicket.categoria != "",
                )
                .distinct()
                .all()
            )
            categorias_set.update(item[0] for item in categorias_en_bd if item and item[0])
        elif current_user.tipo_chat == "pyme" and current_user.rubro_id:
            categorias_en_bd = (
                db.session.query(PymeTicket.categoria)
                .filter(
                    PymeTicket.rubro_id == current_user.rubro_id,
                    PymeTicket.categoria.isnot(None),
                    PymeTicket.categoria != "",
                )
                .distinct()
                .all()
            )
            categorias_set.update(item[0] for item in categorias_en_bd if item and item[0])
    except Exception:
        # Si hay algún problema consultando la base, devolvemos las categorías
        # base en lugar de propagar un error al frontend.
        categorias_set = categorias_set or set()

    categorias = [
        {"value": c, "label": c.title()} for c in sorted(categorias_set, key=str.casefold)
    ]
    search_term = (request.args.get("q") or "").strip().lower()
    if search_term:
        categorias = [
            item for item in categorias if search_term in item["label"].lower()
        ]
    return jsonify({"categorias": categorias})

@empleados_bp.route('', methods=['POST'])
@token_requerido
@solo_admin_requerido
def crear_empleado(current_user: User):
    """Crea un nuevo empleado asociado al usuario actual."""
    data = request.get_json(silent=True) or {}
    name = data.get('name')
    email = data.get('email')
    password = data.get('password')
    categorias = data.get('categorias')

    categorias_normalizadas = _normalize_categorias_input(categorias)
    if categorias_normalizadas is None or not categorias_normalizadas:
        return jsonify({"error": "Debe asignar al menos una categoría válida"}), 400
    if not all([name, email, password]):
        return jsonify({"error": "Datos inválidos"}), 400
    if User.query.filter_by(email=email.strip().lower()).first():
        return jsonify({"error": "Email ya registrado"}), 400
    nuevo = User(
        name=name.strip(),
        email=email.strip().lower(),
        token=str(uuid.uuid4()),
        rol='empleado',
        empresa_id=current_user.id,
        tipo_chat=current_user.tipo_chat
        or (
            "municipio" if es_rubro_publico(current_user.rubro) else "pyme"
        ),
        ticket_categorias=",".join(categorias_normalizadas),
    )
    nuevo.set_password(password)
    db.session.add(nuevo)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al crear"}), 500
    return jsonify({
        "id": nuevo.id,
        "name": nuevo.name,
        "email": nuevo.email,
        "rol": nuevo.rol,
        "categorias": categorias_normalizadas,
    }), 201

@empleados_bp.route('/<int:emp_id>/historial', methods=['GET'])
@token_requerido
@solo_admin_requerido
def historial_empleado(current_user: User, emp_id: int):
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({'error': 'Empleado no encontrado o no pertenece a su empresa'}), 404

    fecha_inicio_str = request.args.get('fecha_inicio')
    fecha_fin_str = request.args.get('fecha_fin')

    query_comentarios = TicketComentario.query.filter_by(user_id=emp_id, es_admin=True)

    try:
        if fecha_inicio_str:
            fecha_inicio = datetime.fromisoformat(fecha_inicio_str)
            query_comentarios = query_comentarios.filter(TicketComentario.fecha >= fecha_inicio)
        if fecha_fin_str:
            # Para incluir el día completo, podríamos querer ir hasta el final del día
            fecha_fin = datetime.fromisoformat(fecha_fin_str)
            query_comentarios = query_comentarios.filter(TicketComentario.fecha <= fecha_fin)
    except ValueError:
        return jsonify({"error": "Formato de fecha inválido. Usar YYYY-MM-DD o formato ISO."}), 400

    comentarios = query_comentarios.order_by(TicketComentario.fecha.desc()).all()
    historial = []
    for c in comentarios:
        tipo = 'pyme' if c.pyme_ticket_id else 'municipio'
        ticket_id = c.pyme_ticket_id or c.municipio_ticket_id
        historial.append({
            "ticket_id": ticket_id,
            "tipo": tipo,
            "comentario": c.comentario,
            "fecha": c.fecha.isoformat()
        })
    return jsonify(historial)


# --- Nuevas rutas para gestionar empleados ---

@empleados_bp.route('/<int:emp_id>', methods=['GET'])
@token_requerido
@solo_admin_requerido
def obtener_empleado(current_user: User, emp_id: int):
    """Devuelve los datos de un empleado específico."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404

    fecha_inicio_mes = datetime.utcnow() - timedelta(days=30)
    tickets_respondidos_mes = db.session.query(func.count(TicketComentario.id)).filter(
        TicketComentario.user_id == empleado.id,
        TicketComentario.es_admin == True,
        TicketComentario.fecha >= fecha_inicio_mes
    ).scalar() or 0
    ticket_query_base, TicketModel = _build_ticket_query_for_owner(current_user)
    categorias = empleado.ticket_categorias.split(",") if empleado.ticket_categorias else []
    normalized_categories = [c.strip().lower() for c in categorias if c.strip()]
    open_statuses = [estado for estado in TICKET_ALLOWED_STATES if estado != "cerrado"]
    open_tickets = 0
    if ticket_query_base is not None and normalized_categories:
        open_tickets = (
            ticket_query_base.filter(
                TicketModel.estado.in_(open_statuses),
                func.lower(TicketModel.categoria).in_(normalized_categories),
            ).count()
        )

    return jsonify({
        "id": empleado.id,
        "name": empleado.name,
        "email": empleado.email,
        "rol": empleado.rol,
        "categorias": categorias,
        "tickets_respondidos_mes": tickets_respondidos_mes,
        "tickets_abiertos_categoria": open_tickets,
    })


@empleados_bp.route('/<int:emp_id>', methods=['PUT'])
@token_requerido
@solo_admin_requerido
def actualizar_empleado(current_user: User, emp_id: int):
    """Actualiza los datos básicos de un empleado."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    data = request.get_json(silent=True) or {}
    if 'name' in data:
        empleado.name = data['name'].strip()
    if 'email' in data:
        nuevo_email = data['email'].strip().lower()
        if nuevo_email != empleado.email:
            return jsonify({"error": "El email no puede modificarse una vez creado"}), 400
    if 'password' in data and data['password']:
        empleado.set_password(data['password'])
    if 'categorias' in data:
        cats = _normalize_categorias_input(data['categorias'])
        if cats is None or not cats:
            return jsonify({"error": "Debe asignar al menos una categoría válida"}), 400
        empleado.ticket_categorias = ",".join(cats)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al actualizar"}), 500

    # Recalculate open tickets after update
    ticket_query_base, TicketModel = _build_ticket_query_for_owner(current_user)
    categorias = empleado.ticket_categorias.split(",") if empleado.ticket_categorias else []
    normalized_categories = [c.strip().lower() for c in categorias if c.strip()]
    open_statuses = [estado for estado in TICKET_ALLOWED_STATES if estado != "cerrado"]
    open_tickets = 0
    if ticket_query_base is not None and normalized_categories:
        open_tickets = (
            ticket_query_base.filter(
                TicketModel.estado.in_(open_statuses),
                func.lower(TicketModel.categoria).in_(normalized_categories),
            ).count()
        )

    return jsonify({
        "id": empleado.id,
        "name": empleado.name,
        "email": empleado.email,
        "rol": empleado.rol,
        "categorias": categorias,
        "tickets_abiertos_categoria": open_tickets,
    })


@empleados_bp.route('/<int:emp_id>', methods=['DELETE'])
@token_requerido
@solo_admin_requerido
def eliminar_empleado(current_user: User, emp_id: int):
    """Elimina un empleado de la empresa."""
    empleado = User.query.filter_by(id=emp_id, empresa_id=current_user.id, rol='empleado').first()
    if not empleado:
        return jsonify({"error": "Empleado no encontrado"}), 404
    db.session.delete(empleado)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Error al eliminar"}), 500
    return jsonify({"mensaje": "Empleado eliminado"})

