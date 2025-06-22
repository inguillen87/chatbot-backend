from flask import Blueprint, jsonify
from sqlalchemy import text
from routes.auth import token_requerido
from models import db

estadisticas_bp = Blueprint('estadisticas', __name__, url_prefix='/estadisticas')

@estadisticas_bp.route('/reclamos', methods=['GET'])
@token_requerido
def estadisticas_reclamos(current_user):
    """Devuelve métricas básicas de tickets y tiempo de respuesta."""
    if current_user.empresa_id is not None:
        return jsonify({"error": "Permisos insuficientes"}), 403

    datos = {}

    # Cantidad por rubro (solo pymes)
    rows = db.session.execute(
        text(
            "SELECT r.nombre AS rubro, COUNT(*) AS total "
            "FROM pyme_ticket pt JOIN rubro r ON pt.rubro_id = r.id "
            "GROUP BY r.nombre"
        )
    ).fetchall()
    datos['por_rubro'] = [
        {"rubro": r.rubro, "total": r.total} for r in rows
    ]

    # Tickets por tipo
    total_muni = db.session.execute(text("SELECT COUNT(*) FROM municipio_ticket" )).scalar() or 0
    total_pyme = db.session.execute(text("SELECT COUNT(*) FROM pyme_ticket" )).scalar() or 0
    datos['por_tipo'] = [
        {"tipo": "municipio", "total": total_muni},
        {"tipo": "pyme", "total": total_pyme},
    ]

    # Tiempo de respuesta promedio en segundos
    resp_muni = db.session.execute(
        text(
            "SELECT AVG(julianday(tc.fecha) - julianday(mt.fecha)) * 86400 "
            "FROM municipio_ticket mt JOIN ticket_comentario tc ON tc.municipio_ticket_id = mt.id "
            "WHERE tc.es_admin = 1"
        )
    ).scalar()
    resp_pyme = db.session.execute(
        text(
            "SELECT AVG(julianday(tc.fecha) - julianday(pt.fecha)) * 86400 "
            "FROM pyme_ticket pt JOIN ticket_comentario tc ON tc.pyme_ticket_id = pt.id "
            "WHERE tc.es_admin = 1"
        )
    ).scalar()
    datos['tiempo_respuesta_promedio_segundos'] = {
        'municipio': round(resp_muni or 0, 2),
        'pyme': round(resp_pyme or 0, 2),
    }

    return jsonify(datos)
