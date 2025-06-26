from flask import Blueprint, jsonify, request, current_app
from routes.auth import token_requerido, admin_o_empleado_requerido
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
    return jsonify(_obtener_clientes(current_user, tag))

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


@municipal_bp.route('/incidents', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def municipal_incidents(current_user):
    """Lista los tickets municipales abiertos para el municipio del usuario."""

    try:
        tickets = (
            MunicipioTicket.query
            .filter_by(municipio_id=current_user.municipio_id)
            .filter(MunicipioTicket.estado != 'cerrado')
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
            "pregunta": getattr(t, "pregunta", None),
            "detalles": getattr(t, "detalles", None),
            "direccion": getattr(t, "direccion", None),
            "latitud": getattr(t, "latitud", None),
            "longitud": getattr(t, "longitud", None),
            "archivo_url": getattr(t, "archivo_url", None),
        }
        for t in tickets
    ]

    return jsonify(resultado)
