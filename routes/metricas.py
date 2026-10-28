from flask import Blueprint, g, jsonify, request
import uuid
from extensions import db
from datetime import datetime, timedelta
from routes.auth import token_requerido
from utils.plan_limits import limite_para_usuario
from services.metricas_service import MetricasService, MetricsScopeError, business_metrics_actor_can_read
from services.analytics.rbac import legacy_tenant_wide_analytics_denial
from utils.tenant_admin_access import resolve_consistent_user_tenant
from utils.auth_helpers import _explicit_admin_request_tenant
from models import TenantProfile
from services.municipio_metricas_service import MunicipioMetricasService

metricas_bp = Blueprint("metricas_bp", __name__)


def _request_id():
    return (request.headers.get("X-Request-Id") or request.headers.get("X-Correlation-Id")
            or getattr(g, "request_id", None) or uuid.uuid4().hex)


def _employee_analytics_denial(actor):
    request_id = _request_id()
    payload = legacy_tenant_wide_analytics_denial(actor, request_id=request_id)
    if payload is None:
        return None
    response = jsonify(payload)
    response.status_code = 403
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return response


def _pyme_service(actor):
    explicit, had_hint = _explicit_admin_request_tenant()
    exact_id = getattr(actor, "tenant_id", None)
    tenant = (explicit if had_hint else db.session.get(TenantProfile, exact_id)
              if exact_id is not None else resolve_consistent_user_tenant(actor))
    if not business_metrics_actor_can_read(actor, tenant, explicit_selection=had_hint):
        tenant = None
    if tenant is not None:
        try:
            return MetricasService(pyme_id=tenant.pyme_id, tenant_id=tenant.id), None
        except MetricsScopeError:
            pass
    request_id = _request_id()
    response = jsonify({
        "contract_version": "shared.error.v1", "ok": False, "status_code": 403,
        "reason_code": "metrics_tenant_scope_unavailable", "retryable": False,
        "request_id": request_id, "action_hint": "select_authorized_tenant",
        "error": {"code": 403, "message": "No se pudo verificar la organizacion de estas metricas."},
    })
    response.status_code = 403
    response.headers["X-Request-Id"] = request_id
    response.headers["Cache-Control"] = "no-store"
    return None, response



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
    if not usuario_actual.is_authenticated:
        return jsonify({"error": "No autorizado"}), 403

    service, scope_error = _pyme_service(usuario_actual)
    if scope_error is not None:
        return scope_error

    return jsonify(service.get_summary())


@metricas_bp.route('/api/municipal/metrics/summary', methods=['GET'])
@metricas_bp.route('/api/municipal/metricas/summary', methods=['GET'])
@token_requerido
def get_municipal_metrics_summary(usuario_actual):
    """Resumen de métricas para municipios."""
    denial = _employee_analytics_denial(usuario_actual)
    if denial is not None:
        return denial
    if (
        not usuario_actual.is_authenticated
        or not hasattr(usuario_actual, 'municipio_id')
        or not usuario_actual.municipio_id
    ):
        return jsonify({"error": "No autorizado"}), 403

    service = MunicipioMetricasService(
        municipio_id=usuario_actual.municipio_id,
        tenant_id=getattr(usuario_actual, "tenant_id", None),
    )
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
    if not usuario_actual.is_authenticated:
        return jsonify({"error": "No autorizado"}), 403

    service, scope_error = _pyme_service(usuario_actual)
    if scope_error is not None:
        return scope_error
    kpis = service.get_kpis()
    return jsonify(kpis)

@metricas_bp.route('/api/metrics/sales-over-time', methods=['GET'])
@token_requerido
def get_sales_over_time(usuario_actual):
    """
    Endpoint de API para obtener las ventas a lo largo del tiempo.
    """
    if not usuario_actual.is_authenticated:
        return jsonify({"error": "No autorizado"}), 403

    service, scope_error = _pyme_service(usuario_actual)
    if scope_error is not None:
        return scope_error
    sales_over_time = service.get_sales_over_time()
    response = jsonify(sales_over_time)
    response.headers["X-Metrics-Availability"] = "unavailable"
    response.headers["X-Metrics-Reason"] = "sales_timeseries_not_supported"
    return response

@metricas_bp.route('/api/metrics/top-products', methods=['GET'])
@token_requerido
def get_top_products(usuario_actual):
    """
    Endpoint de API para obtener los productos más vendidos.
    """
    if not usuario_actual.is_authenticated:
        return jsonify({"error": "No autorizado"}), 403

    service, scope_error = _pyme_service(usuario_actual)
    if scope_error is not None:
        return scope_error
    top_products = service.get_top_products()
    response = jsonify(top_products)
    response.headers["X-Metrics-Availability"] = "unavailable"
    response.headers["X-Metrics-Reason"] = "product_sales_not_supported"
    return response

@metricas_bp.route('/api/metrics/sales-by-region', methods=['GET'])
@token_requerido
def get_sales_by_region(usuario_actual):
    """
    Endpoint de API para obtener las ventas por región.
    """
    if not usuario_actual.is_authenticated:
        return jsonify({"error": "No autorizado"}), 403

    service, scope_error = _pyme_service(usuario_actual)
    if scope_error is not None:
        return scope_error
    sales_by_region = service.get_sales_by_region()
    response = jsonify(sales_by_region)
    response.headers["X-Metrics-Availability"] = "unavailable"
    response.headers["X-Metrics-Reason"] = "regional_sales_not_supported"
    return response
