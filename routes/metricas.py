from flask import Blueprint, jsonify, request
from extensions import db
from sqlalchemy import func
from datetime import datetime, timedelta
from routes.auth import token_requerido

metricas_bp = Blueprint("metricas_bp", __name__)

@metricas_bp.route("/metricas", methods=["GET"])
@token_requerido
def obtener_metricas(usuario_actual):
    try:
        total = usuario_actual.preguntas_usadas or 0


        # Preguntas esta semana
        desde = datetime.now() - timedelta(days=7)
        preguntas_esta_semana = db.session.execute(
            db.text(
                "SELECT COUNT(*) FROM logs WHERE user_id = :uid AND fecha >= :desde"
            ),
            {"uid": usuario_actual.id, "desde": desde},
        ).scalar() or 0

        # Último uso
        fecha_ultimo_uso = db.session.execute(
            db.text(
                "SELECT MAX(fecha) FROM logs WHERE user_id = :uid"
            ),
            {"uid": usuario_actual.id},
        ).scalar()

        return jsonify({
            "total_preguntas": total,
            "preguntas_esta_semana": preguntas_esta_semana,
            "fecha_ultimo_uso": fecha_ultimo_uso.isoformat() if fecha_ultimo_uso else None
        })

    except Exception as e:
        return jsonify({"error": f"Error al obtener métricas: {str(e)}"}), 500
