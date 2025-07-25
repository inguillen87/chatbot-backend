from flask import Blueprint, jsonify
from sqlalchemy import text
from routes.auth import token_requerido, admin_o_empleado_requerido
from models import db
from services.logic import es_rubro_publico
from datetime import datetime, timedelta

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

        # Estadísticas por categoría para Municipio
        categorias_muni_rows = db.session.execute(
            text(
                "SELECT COALESCE(categoria, 'Sin Categoría') AS categoria, COUNT(*) AS total "
                "FROM municipio_ticket "
                "WHERE municipio_id = :mid "
                "GROUP BY COALESCE(categoria, 'Sin Categoría')"
            ),
            {"mid": mid}
        ).fetchall()
        datos["por_categoria_municipio"] = [
            {"categoria": row.categoria, "total": row.total} for row in categorias_muni_rows
        ]

        datos["por_tipo"] = [{"tipo": "municipio", "total": total_muni}]
        datos["tiempo_respuesta_promedio_segundos"] = {
            "municipio": round(resp_muni or 0, 2)
        }

        # Tickets por día (últimos 30 días)
        fecha_fin_dia = datetime.utcnow()
        fecha_inicio_dia = fecha_fin_dia - timedelta(days=30)
        tickets_por_dia_muni = db.session.execute(
            text(
                "SELECT DATE(fecha) AS dia, COUNT(*) AS total "
                "FROM municipio_ticket "
                "WHERE municipio_id = :mid AND fecha BETWEEN :inicio AND :fin "
                "GROUP BY dia ORDER BY dia ASC"
            ),
            {"mid": mid, "inicio": fecha_inicio_dia.strftime('%Y-%m-%d %H:%M:%S'), "fin": fecha_fin_dia.strftime('%Y-%m-%d %H:%M:%S')}
        ).fetchall()
        datos["tickets_por_dia"] = [{"dia": row.dia, "total": row.total} for row in tickets_por_dia_muni]

        # Tickets por mes (últimos 12 meses)
        fecha_fin_mes = datetime.utcnow()
        fecha_inicio_mes = fecha_fin_mes - timedelta(days=365) # Aproximado
        tickets_por_mes_muni = db.session.execute(
            text(
                "SELECT STRFTIME('%Y-%m', fecha) AS mes, COUNT(*) AS total "
                "FROM municipio_ticket "
                "WHERE municipio_id = :mid AND fecha BETWEEN :inicio AND :fin "
                "GROUP BY mes ORDER BY mes ASC"
            ),
            {"mid": mid, "inicio": fecha_inicio_mes.strftime('%Y-%m-%d %H:%M:%S'), "fin": fecha_fin_dia.strftime('%Y-%m-%d %H:%M:%S')} # Usa fecha_fin_dia para el fin del rango de mes también
        ).fetchall()
        datos["tickets_por_mes"] = [{"mes": row.mes, "total": row.total} for row in tickets_por_mes_muni]


    else: # PYME
        rid = current_user.rubro_id
        if not rid: # Sanity check, admin/empleado pyme debería tener rubro_id
            return jsonify({"error": "Usuario PYME no tiene rubro asignado."}), 400

        # Estadísticas por categoría para PYME
        categorias_pyme_rows = db.session.execute(
            text(
                "SELECT COALESCE(categoria, 'Sin Categoría') AS categoria, COUNT(*) AS total "
                "FROM pyme_ticket "
                "WHERE rubro_id = :rid "
                "GROUP BY COALESCE(categoria, 'Sin Categoría')"
            ),
            {"rid": rid}
        ).fetchall()
        datos["por_categoria_pyme"] = [
            {"categoria": row.categoria, "total": row.total} for row in categorias_pyme_rows
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

        # Tickets por día (últimos 30 días) para PYME
        fecha_fin_dia = datetime.utcnow()
        fecha_inicio_dia = fecha_fin_dia - timedelta(days=30)
        tickets_por_dia_pyme = db.session.execute(
            text(
                "SELECT DATE(fecha) AS dia, COUNT(*) AS total "
                "FROM pyme_ticket "
                "WHERE rubro_id = :rid AND fecha BETWEEN :inicio AND :fin "
                "GROUP BY dia ORDER BY dia ASC"
            ),
            {"rid": rid, "inicio": fecha_inicio_dia.strftime('%Y-%m-%d %H:%M:%S'), "fin": fecha_fin_dia.strftime('%Y-%m-%d %H:%M:%S')}
        ).fetchall()
        datos["tickets_por_dia"] = [{"dia": row.dia, "total": row.total} for row in tickets_por_dia_pyme]

        # Tickets por mes (últimos 12 meses) para PYME
        fecha_fin_mes = datetime.utcnow()
        fecha_inicio_mes = fecha_fin_mes - timedelta(days=365)
        tickets_por_mes_pyme = db.session.execute(
            text(
                "SELECT STRFTIME('%Y-%m', fecha) AS mes, COUNT(*) AS total "
                "FROM pyme_ticket "
                "WHERE rubro_id = :rid AND fecha BETWEEN :inicio AND :fin "
                "GROUP BY mes ORDER BY mes ASC"
            ),
            {"rid": rid, "inicio": fecha_inicio_mes.strftime('%Y-%m-%d %H:%M:%S'), "fin": fecha_fin_dia.strftime('%Y-%m-%d %H:%M:%S')}
        ).fetchall()
        datos["tickets_por_mes"] = [{"mes": row.mes, "total": row.total} for row in tickets_por_mes_pyme]

    return jsonify(datos)
