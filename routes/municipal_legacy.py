from flask import Blueprint, jsonify, request, current_app
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido
from datetime import datetime, timedelta
from utils.permissions import require_role
from routes.crm import _obtener_clientes
from services.municipios import TODAS_LAS_CATEGORIAS_UNICAS
from routes.tramites import listar_tramites, obtener_tramite
from models import MunicipioTicket, db
from sqlalchemy import text

municipal_bp = Blueprint('municipal_legacy', __name__, url_prefix='/municipal')

@municipal_bp.route('/usuarios', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_usuarios(current_user):
    tag = request.args.get('tag')
    q = request.args.get('q')
    marketing = request.args.get('acepta_marketing')
    sort = request.args.get('sort')
    order = request.args.get('order')
    limit = request.args.get('limit')
    offset = request.args.get('offset')
    return jsonify(
        _obtener_clientes(
            current_user,
            tag,
            q=q,
            acepta_marketing=marketing,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )
    )

@municipal_bp.route('/categorias', methods=['GET'])
@token_requerido
@require_role('admin', 'empleado')
def municipal_categorias(current_user):
    return jsonify(TODAS_LAS_CATEGORIAS_UNICAS)

@municipal_bp.route('/stats', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats(current_user):
    """Estadísticas profesionales del municipio del usuario."""

    mid = current_user.municipio_id

    abiertos = db.session.execute(
        text(
            "SELECT COUNT(*) FROM municipio_ticket "
            "WHERE municipio_id = :mid AND estado != 'cerrado'"
        ),
        {"mid": mid},
    ).scalar() or 0

    cerrados = db.session.execute(
        text(
            "SELECT COUNT(*) FROM municipio_ticket "
            "WHERE municipio_id = :mid AND estado = 'cerrado'"
        ),
        {"mid": mid},
    ).scalar() or 0

    rows = db.session.execute(
        text(
            "SELECT categoria, "
            "SUM(CASE WHEN estado != 'cerrado' THEN 1 ELSE 0 END) AS abiertos, "
            "SUM(CASE WHEN estado = 'cerrado' THEN 1 ELSE 0 END) AS cerrados "
            "FROM municipio_ticket WHERE municipio_id = :mid GROUP BY categoria"
        ),
        {"mid": mid},
    ).fetchall()
    por_categoria = [
        {
            "categoria": r.categoria,
            "abiertos": r.abiertos,
            "cerrados": r.cerrados,
        }
        for r in rows
    ]

    tiempo_respuesta = db.session.execute(
        text(
            "SELECT AVG(julianday(tc.fecha) - julianday(mt.fecha)) * 86400 "
            "FROM municipio_ticket mt JOIN ticket_comentario tc "
            "ON tc.municipio_ticket_id = mt.id "
            "WHERE tc.es_admin = 1 AND mt.municipio_id = :mid"
        ),
        {"mid": mid},
    ).scalar()

    datos = {
        "totales": {"abiertos": abiertos, "cerrados": cerrados},
        "por_categoria": por_categoria,
        "tiempo_respuesta_promedio_segundos": round(tiempo_respuesta or 0, 2),
    }

    return jsonify(datos)

@municipal_bp.route('/stats/filters', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_stats_filters(current_user):
    return jsonify({'categorias': TODAS_LAS_CATEGORIAS_UNICAS})

@municipal_bp.route('/tramites', methods=['GET'])
def municipal_tramites():
    return listar_tramites()

@municipal_bp.route('/tramites/<string:nombre>', methods=['GET'])
def municipal_tramite(nombre):
    return obtener_tramite(nombre)

@municipal_bp.route('/tickets/map_data', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_map_data(current_user):
    """
    Devuelve datos de tickets municipales abiertos con ubicación
    para el municipio del usuario actual, optimizado para mostrar en un mapa.
    """
    from services.ticket_service import servicio_tickets # Importación local

    municipio_id_del_admin = current_user.municipio_id
    if not municipio_id_del_admin:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    tickets_con_ubicacion = servicio_tickets.obtener_tickets_con_ubicacion_para_mapa(
        tipo_ticket="municipio",
        municipio_id=municipio_id_del_admin,
        estado="abierto"  # Explicitly request open tickets for map data consistency
    )
    return jsonify(tickets_con_ubicacion)


@municipal_bp.route('/tickets/locations', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_tickets_locations(current_user):
    """
    Devuelve una lista de coordenadas de tickets para el mapa de calor.
    Formato: [{ "lat": lat, "lng": lng }]
    """
    from services.ticket_service import servicio_tickets

    municipio_id_del_admin = current_user.municipio_id
    if not municipio_id_del_admin:
        return jsonify({"error": "Usuario no asociado a un municipio"}), 400

    locations = servicio_tickets.obtener_locations_de_tickets(
        municipio_id=municipio_id_del_admin
    )
    return jsonify(locations)


@municipal_bp.route('/incidents', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_incidents(current_user):
    """Lista los tickets municipales abiertos para el municipio del usuario."""

    try:
        tickets = (
            MunicipioTicket.query
            .filter_by(municipio_id=current_user.municipio_id)
            .filter(MunicipioTicket.estado != 'cerrado') # Podríamos querer ver todos en el admin, no solo los no cerrados
            .order_by(MunicipioTicket.fecha.desc())
            .all()
        )
    except Exception:
        current_app.logger.exception("Error fetching municipal incidents")
        tickets = []

    resultado = [
        {
            "id": t.id,
            "nro_ticket": t.nro_ticket,
            "asunto": getattr(t, "asunto", "N/A"),
            "categoria": getattr(t, "categoria", None),
            "estado": t.estado,
            "fecha": t.fecha.isoformat() if getattr(t, "fecha", None) else None,
            "pregunta": getattr(t, "pregunta", None), # Descripción breve inicial
            "detalles": getattr(t, "detalles", None), # Detalles completos del reclamo
            "direccion": getattr(t, "direccion", None),
            "latitud": getattr(t, "latitud", None),
            "longitud": getattr(t, "longitud", None),
            "archivo_url": getattr(t, "archivo_url", None), # Para la foto
            # Datos del vecino/usuario si están disponibles (requeriría join con User o guardar en ticket)
            "nombre_vecino": getattr(t, "nombre_vecino", None), # Asumiendo que se añada al modelo o se obtenga de User
            "telefono_vecino": getattr(t, "telefono_vecino", None),
            "email_vecino": getattr(t, "email_vecino", None),
        }
        for t in tickets
    ]

    return jsonify(resultado)


def _municipal_message_metrics(eid: int) -> list[dict]:
    """Calcula métricas de mensajes recibidos en distintos períodos."""

    ahora = datetime.now()

    def _contar_desde(dias: int) -> int:
        desde = ahora - timedelta(days=dias)
        return (
            db.session.execute(
                db.text(
                    "SELECT COUNT(*) FROM conversacion c JOIN user u ON c.user_id = u.id "
                    "WHERE u.empresa_id = :eid AND c.timestamp >= :desde"
                ),
                {"eid": eid, "desde": desde},
            ).scalar()
            or 0
        )

    return [
        {"label": "Mensajes esta semana", "value": _contar_desde(7)},
        {"label": "Mensajes este mes", "value": _contar_desde(30)},
        {"label": "Mensajes este año", "value": _contar_desde(365)},
    ]


@municipal_bp.route('/metrics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_metrics(current_user):
    """Devuelve cantidad de mensajes de vecinos por rango de tiempo."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))


@municipal_bp.route('/posts', methods=['POST'])
@token_requerido
@admin_o_empleado_requerido
def create_municipal_post(current_user):
    """
    Crea un nuevo post municipal (evento, noticia, etc.).
    Utiliza el modelo MunicipioTicket con una categoría específica.
    """
    if current_user.tipo_chat != "municipio":
        return jsonify({"error": "Acceso denegado. Se requiere un usuario municipal."}), 403

    # El frontend envía 'titulo' y 'descripcion' en un FormData.
    titulo = request.form.get('titulo')
    descripcion = request.form.get('descripcion')
    categoria = request.form.get('categoria', 'noticia') # Default a 'noticia'

    if not titulo or not descripcion:
        return jsonify({"error": "El título y la descripción son requeridos."}), 400

    try:
        nuevo_post = MunicipioTicket(
            asunto=titulo,
            detalles=descripcion,
            pregunta=descripcion, # Llenamos pregunta para consistencia
            categoria=categoria,
            municipio_id=current_user.municipio_id,
            user_id=current_user.id, # El admin que lo crea
            estado='publicado', # Un estado específico para posts
            nombre_vecino=current_user.name, # Nombre del admin
            email_vecino=current_user.email, # Email del admin
        )
        db.session.add(nuevo_post)
        db.session.commit()

        # Serializar el post creado para la respuesta
        # Usamos una serialización simple aquí, podría crearse una dedicada si es necesario
        post_data = {
            "id": nuevo_post.id,
            "titulo": nuevo_post.asunto,
            "descripcion": nuevo_post.detalles,
            "categoria": nuevo_post.categoria,
            "fecha": nuevo_post.fecha.isoformat(),
            "estado": nuevo_post.estado,
            "autor_id": nuevo_post.user_id,
        }

        return jsonify(post_data), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Error al crear post municipal: {e}", exc_info=True)
        return jsonify({"error": "Error interno al crear el post."}), 500


@municipal_bp.route('/analytics', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_analytics(current_user):
    """Alias de ``/metrics`` para compatibilidad con el frontend."""

    eid = current_user.id if current_user.empresa_id is None else current_user.empresa_id
    return jsonify(_municipal_message_metrics(eid))
