from pathlib import Path

from flask import Flask

from config import Config
from cutover_writer_fence import cutover_writer_view, is_cutover_writer_view
from middleware.cutover_writer_fence import register_cutover_writer_fence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _set_fence(client, enabled: bool) -> bool:
    previous = client.application.config.get("CUTOVER_WRITER_FENCE_ENABLED")
    client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = enabled
    return previous


def test_cutover_writer_fence_defaults_fail_open_for_normal_operation():
    assert Config.CUTOVER_WRITER_FENCE_ENABLED is False
    assert (
        "CUTOVER_WRITER_FENCE_ENABLED=false"
        in (REPOSITORY_ROOT / ".env.example").read_text(encoding="utf-8")
    )


def test_cutover_writer_fence_blocks_mutating_methods_before_route_handlers(client):
    previous = _set_fence(client, True)
    try:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            response = client.open("/api/cutover-probe", method=method)

            assert response.status_code == 503
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Retry-After"] == "60"
            assert response.get_json() == {
                "contract_version": "cutover.writer_fence.v1",
                "status": "maintenance",
                "reason_code": "cutover_writer_fence_enabled",
                "retryable": True,
            }
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_cutover_writer_fence_keeps_health_and_safe_methods_available(client):
    previous = _set_fence(client, True)
    try:
        assert client.get("/health").status_code == 200
        assert client.head("/health").status_code == 200
        assert client.open("/api/cutover-probe", method="OPTIONS").status_code != 503
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_marked_get_and_head_are_fenced_before_handler_but_read_only_get_remains_available():
    app = Flask(__name__)
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    calls = {"read": 0, "write": 0}
    register_cutover_writer_fence(app)

    @app.get("/read-only")
    def read_only():
        calls["read"] += 1
        return {"ok": True}

    @app.get("/mutating-read")
    @cutover_writer_view
    def mutating_read():
        calls["write"] += 1
        raise AssertionError("marked writer view must never execute")

    test_client = app.test_client()
    assert test_client.get("/read-only").status_code == 200
    assert test_client.get("/mutating-read").status_code == 503
    assert test_client.head("/mutating-read").status_code == 503
    assert calls == {"read": 1, "write": 0}


def test_all_known_mutating_get_endpoints_are_explicitly_marked(client):
    expected = {
        "admin_tenant_bp.admin_tenant_catalog",
        "analytics.analytics_identity_coverage",
        "auth.verify_email",
        "carrito_bp.carrito_pwa_public",
        "carrito_bp.carrito_root",
        "carrito_bp.resumen",
        "catalogo.listar_catalogo",
        "conversations_bp.get_conversation_timeline",
        "conversations_bp.get_link_request_status",
        "education.get_education_operations_heatmap",
        "education.get_education_operations_summary",
        "education.get_family_context",
        "education.get_school_case_detail",
        "education.list_school_cases",
        "encuestas_analytics_admin_bp.export_pdf_view",
        "encuestas_analytics_admin_bp.export_view",
        "encuestas_analytics_bp.export_pdf_view",
        "encuestas_analytics_bp.export_view",
        "encuestas_analytics_legacy_bp.export_pdf_view",
        "encuestas_analytics_legacy_bp.export_view",
        "encuestas_analytics_municipal_bp.export_pdf_view",
        "encuestas_analytics_municipal_bp.export_view",
        "municipio_api.obtener_widget_config",
        "legacy_public_api_v2.legacy_productos_publicos",
        "market.public_cart_summary",
        "market.public_catalog",
        "market.public_product_detail",
        "notifications.get_notification_detail",
        "notifications.list_notification_templates",
        "notifications.notification_alerts",
        "notifications.notification_metrics",
        "productos.obtener_productos",
        "public_tenant_bp.download_catalog",
        "public_tenant_bp.get_catalog",
        "pwa_public.public_cart_summary",
        "pwa_public.public_catalog",
        "v2_saas.whatsapp_tech_provider_sender_status_v2",
        "v2_tickets.list_tickets_v2",
        "whatsapp_rules_bp.get_rules",
        "widget_public_config.obtener_config_publica",
        "widget_settings.manage_settings",
    }

    missing = expected.difference(client.application.view_functions)
    assert missing == set()
    assert all(
        is_cutover_writer_view(client.application.view_functions[endpoint])
        for endpoint in expected
    )


def test_disabled_cutover_writer_fence_does_not_replace_normal_routing(client):
    previous = _set_fence(client, False)
    try:
        assert client.post("/api/cutover-probe").status_code == 404
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous
