from flask import Blueprint, jsonify
from sqlalchemy import text
from routes.auth import token_requerido, admin_o_empleado_requerido
from models import db
from services.logic import es_rubro_publico

estadisticas_bp = Blueprint('estadisticas', __name__, url_prefix='/estadisticas')

@estadisticas_bp.route('/reclamos', methods=['GET'])
@token_requerido
@admin_o_empleado_requerido
def estadisticas_reclamos(current_user):
    """Devuelve métricas básicas de tickets y tiempo de respuesta."""

    datos = {}

    es_municipio = es_rubro_publico(getattr(current_user, "rubro", None))

    if es_municipio:
        mid = current_user.municipio_id

        total_muni = db.session.execute(
            text("SELECT COUNT(*) FROM municipio_ticket WHERE municipio_id = :mid"),
            {"mid": mid},
        ).scalar() or 0

        resp_muni = db.session.execute(
            text(
                "SELECT AVG(julianday(tc.fecha) - julianday(mt.fecha)) * 86400 "
                "FROM municipio_ticket mt JOIN ticket_comentario tc ON tc.municipio_ticket_id = mt.id "
                "WHERE tc.es_admin = 1 AND mt.municipio_id = :mid"
            ),
            {"mid": mid},
        ).scalar()

        datos["por_rubro"] = []
        datos["por_tipo"] = [{"tipo": "municipio", "total": total_muni}]
        datos["tiempo_respuesta_promedio_segundos"] = {
            "municipio": round(resp_muni or 0, 2)
        }

    else:
        rid = current_user.rubro_id

        rows = db.session.execute(
            text(
                "SELECT r.nombre AS rubro, COUNT(*) AS total "
                "FROM pyme_ticket pt JOIN rubro r ON pt.rubro_id = r.id "
                "WHERE pt.rubro_id = :rid GROUP BY r.nombre"
            ),
            {"rid": rid},
        ).fetchall()
        datos["por_rubro"] = [
            {"rubro": r.rubro, "total": r.total} for r in rows
        ]

        total_pyme = db.session.execute(
            text("SELECT COUNT(*) FROM pyme_ticket WHERE rubro_id = :rid"),
            {"rid": rid},
        ).scalar() or 0

        resp_pyme = db.session.execute(
            text(
                "SELECT AVG(julianday(tc.fecha) - julianday(pt.fecha)) * 86400 "
                "FROM pyme_ticket pt JOIN ticket_comentario tc ON tc.pyme_ticket_id = pt.id "
                "WHERE tc.es_admin = 1 AND pt.rubro_id = :rid"
            ),
            {"rid": rid},
        ).scalar()

        datos["por_tipo"] = [{"tipo": "pyme", "total": total_pyme}]
        datos["tiempo_respuesta_promedio_segundos"] = {
            "pyme": round(resp_pyme or 0, 2)
        }

    return jsonify(datos)
