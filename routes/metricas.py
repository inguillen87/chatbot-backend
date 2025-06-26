from flask import Blueprint, jsonify, request
from extensions import db
from sqlalchemy import func
from datetime import datetime, timedelta
from routes.auth import token_requerido # Asegúrate de que esta importación sea correcta para tu estructura

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

        # Último uso - Si quieres mostrar esto como una métrica, deberías decidir cómo formatearla
        # o mostrarla por separado en el frontend, ya que no encaja en label/value de un gráfico de barras.
        # Por ahora, nos centraremos en las métricas numéricas para el gráfico.
        # fecha_ultimo_uso = db.session.execute(
        #     db.text(
        #         "SELECT MAX(fecha) FROM logs WHERE user_id = :uid"
        #     ),
        #     {"uid": usuario_actual.id},
        # ).scalar()

        # --- MODIFICACIÓN CLAVE AQUÍ ---
        # Devolver las métricas como una lista de objetos {label, value}
        metrics_list = [
            {"label": "Total de Preguntas", "value": total},
            {"label": "Preguntas esta semana", "value": preguntas_esta_semana},
            # Puedes añadir más métricas numéricas aquí si las calculas
        ]

        return jsonify(metrics_list) # Devuelve la lista directamente
        # --- FIN MODIFICACIÓN CLAVE ---

    except Exception as e:
        # Asegúrate de que los errores se manejen de forma segura en producción (ej. no exponiendo detalles internos)
        return jsonify({"error": f"Error al obtener métricas: {str(e)}"}), 500