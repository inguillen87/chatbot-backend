from flask import Blueprint, jsonify
from extensions import db
from datetime import datetime, timedelta
from routes.auth import token_requerido
from utils.plan_limits import limite_para_usuario

metricas_bp = Blueprint("metricas_bp", __name__)


def _compilar_metricas(usuario):
    """Compila la información de métricas para un usuario."""

    total = usuario.preguntas_usadas or 0

    desde = datetime.now() - timedelta(days=7)
    preguntas_esta_semana = db.session.execute(
        db.text(
            "SELECT COUNT(*) FROM logs WHERE user_id = :uid AND fecha >= :desde"
        ),
        {"uid": usuario.id, "desde": desde},
    ).scalar() or 0

    limite = limite_para_usuario(usuario)
    restantes = None
    porcentaje = None
    if limite is not None:
        restantes = max(limite - total, 0)
        porcentaje = round(total / limite * 100, 2)

    last_reset = getattr(usuario, "last_reset", None)

    return [
        {"label": "Total de Preguntas", "value": total, "porcentaje": porcentaje},
        {"label": "Preguntas esta semana", "value": preguntas_esta_semana},
        {"label": "Preguntas restantes", "value": restantes},
        {
            "label": "Fecha último reinicio",
            "value": last_reset.isoformat() if last_reset else None,
        },
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
