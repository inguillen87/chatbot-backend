from flask import Blueprint, jsonify
from extensions import db
from datetime import datetime, timedelta
from routes.auth import token_requerido
from utils.plan_limits import limite_para_usuario
from services.metricas_service import MetricasService
from services.municipio_metricas_service import MunicipioMetricasService

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


@metricas_bp.route('/api/metrics/summary', methods=['GET'])
@token_requerido
def get_metrics_summary(usuario_actual):
    """
    Endpoint de API para obtener un resumen de las métricas.
    """
    if not usuario_actual.is_authenticated or not hasattr(usuario_actual, 'pyme_id') or not usuario_actual.pyme_id:
        return jsonify({"error": "No autorizado"}), 403

    service = MetricasService(pyme_id=usuario_actual.pyme_id)

    summary = {
        "total_sales": service.get_total_ingresos(),
        "total_orders": service.get_total_pedidos(),
        "new_customers": service.get_new_customers(),
        "conversion_rate": service.get_conversion_rate(),
    }
    return jsonify(summary)


@metricas_bp.route('/api/municipal/metrics/summary', methods=['GET'])
@metricas_bp.route('/api/municipal/metricas/summary', methods=['GET'])
@token_requerido
def get_municipal_metrics_summary(usuario_actual):
    """Resumen de métricas para municipios."""
    if (
        not usuario_actual.is_authenticated
        or not hasattr(usuario_actual, 'municipio_id')
        or not usuario_actual.municipio_id
    ):
        return jsonify({"error": "No autorizado"}), 403

    service = MunicipioMetricasService(municipio_id=usuario_actual.municipio_id)
    summary = {
        "total_tickets": service.get_total_tickets(),
        "open_tickets": service.get_open_tickets(),
        "closed_tickets": service.get_closed_tickets(),
        "unique_citizens": service.get_unique_citizens(),
        "resolution_rate": service.get_resolution_rate(),
    }
    return jsonify(summary)

@metricas_bp.route('/api/metrics/kpis', methods=['GET'])
@token_requerido
def get_metrics_kpis(usuario_actual):
    """
    Endpoint de API para obtener los KPIs.
    """
    if not usuario_actual.is_authenticated or not hasattr(usuario_actual, 'pyme_id') or not usuario_actual.pyme_id:
        return jsonify({"error": "No autorizado"}), 403

    service = MetricasService(pyme_id=usuario_actual.pyme_id)
    kpis = service.get_kpis()
    return jsonify(kpis)

@metricas_bp.route('/api/metrics/sales-over-time', methods=['GET'])
@token_requerido
def get_sales_over_time(usuario_actual):
    """
    Endpoint de API para obtener las ventas a lo largo del tiempo.
    """
    if not usuario_actual.is_authenticated or not hasattr(usuario_actual, 'pyme_id') or not usuario_actual.pyme_id:
        return jsonify({"error": "No autorizado"}), 403

    service = MetricasService(pyme_id=usuario_actual.pyme_id)
    sales_over_time = service.get_sales_over_time()
    return jsonify(sales_over_time)

@metricas_bp.route('/api/metrics/top-products', methods=['GET'])
@token_requerido
def get_top_products(usuario_actual):
    """
    Endpoint de API para obtener los productos más vendidos.
    """
    if not usuario_actual.is_authenticated or not hasattr(usuario_actual, 'pyme_id') or not usuario_actual.pyme_id:
        return jsonify({"error": "No autorizado"}), 403

    service = MetricasService(pyme_id=usuario_actual.pyme_id)
    top_products = service.get_top_products()
    return jsonify(top_products)

@metricas_bp.route('/api/metrics/sales-by-region', methods=['GET'])
@token_requerido
def get_sales_by_region(usuario_actual):
    """
    Endpoint de API para obtener las ventas por región.
    """
    if not usuario_actual.is_authenticated or not hasattr(usuario_actual, 'pyme_id') or not usuario_actual.pyme_id:
        return jsonify({"error": "No autorizado"}), 403

    service = MetricasService(pyme_id=usuario_actual.pyme_id)
    sales_by_region = service.get_sales_by_region()
    return jsonify(sales_by_region)
