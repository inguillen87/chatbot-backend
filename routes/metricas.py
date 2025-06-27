from flask import Blueprint, jsonify
from extensions import db
from datetime import datetime, timedelta
from routes.auth import token_requerido

metricas_bp = Blueprint("metricas_bp", __name__)


def _compilar_metricas(usuario):
    total = usuario.preguntas_usadas or 0
    desde = datetime.now() - timedelta(days=7)
    preguntas_esta_semana = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM logs WHERE user_id = :uid AND fecha >= :desde"
        ),
        {"uid": usuario.id, "desde": desde},
    ).scalar() or 0
    return [
        {"label": "Total de Preguntas", "value": total},
        {"label": "Preguntas esta semana", "value": preguntas_esta_semana},
    ]


@metricas_bp.route("/metricas", methods=["GET"])
@token_requerido
def obtener_metricas(usuario_actual):
    try:
        return jsonify(_compilar_metricas(usuario_actual))
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"error": f"Error al obtener métricas: {str(e)}"}), 500


@metricas_bp.route("/pyme/metrics", methods=["GET"])
@token_requerido
def obtener_metricas_pyme(usuario_actual):
    try:
        return jsonify(_compilar_metricas(usuario_actual))
    except Exception as e:  # pragma: no cover - defensive
        return jsonify({"error": f"Error al obtener métricas: {str(e)}"}), 500
