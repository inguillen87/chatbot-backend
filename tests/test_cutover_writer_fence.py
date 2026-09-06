from pathlib import Path

from flask import Flask

from config import Config
from cutover_writer_fence import (
    cutover_read_only_view,
    cutover_writer_view,
    is_cutover_writer_view,
)
from middleware.cutover_writer_fence import register_cutover_writer_fence
from models import User
from services import tenant_resolver


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


def test_cutover_writer_fence_keeps_read_only_demo_session_bootstrap_available(client):
    previous = _set_fence(client, True)
    try:
        response = client.post(
            "/api/v2/demo/session",
            json={"sector": "empresas", "response_profile": "widget"},
        )

        assert response.status_code == 200
        assert response.get_json()["contract_version"] == "demo.session.v2"
        assert response.get_json()["next_step"] == "select_rubro"
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


def test_explicit_read_only_post_remains_available_while_other_posts_are_fenced():
    app = Flask(__name__)
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    calls = {"read_only_post": 0, "writer_post": 0}
    register_cutover_writer_fence(app)

    @app.post("/read-only-evidence")
    @cutover_read_only_view
    def read_only_evidence():
        calls["read_only_post"] += 1
        return {"read_only": True}

    @app.post("/writer")
    def writer():
        calls["writer_post"] += 1
        raise AssertionError("unmarked POST must never execute")

    test_client = app.test_client()
    assert test_client.post("/read-only-evidence", json={}).status_code == 200
    assert test_client.post("/writer", json={}).status_code == 503
    assert calls == {"read_only_post": 1, "writer_post": 0}


def test_contradictory_read_only_and_writer_markers_fail_closed():
    app = Flask(__name__)
    app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    calls = {"dual_marked": 0}
    register_cutover_writer_fence(app)

    @app.post("/contradictory-evidence")
    @cutover_read_only_view
    @cutover_writer_view
    def contradictory_evidence():
        calls["dual_marked"] += 1
        raise AssertionError("a dual-marked view must remain fenced")

    response = app.test_client().post("/contradictory-evidence", json={})

    assert response.status_code == 503
    assert response.get_json()["reason_code"] == "cutover_writer_fence_enabled"
    assert calls == {"dual_marked": 0}


def test_all_known_mutating_get_endpoints_are_explicitly_marked(client):
    expected = {
        "admin_tenant_bp.admin_tenant_catalog",
        "admin_tenant_bp.preview_integration_sync",
        "admin_tenant_bp.tenant_dashboard_bundle",
        "admin_tenant_bp.tenant_unread_ticket_summary",
        "admin_ai_bp.ticket_ai_enrichment",
        "admin_ai_bp.ai_provider_status",
        "analytics.analytics_identity_coverage",
        "auth.verify_email",
        "carrito_bp.carrito_pwa_public",
        "carrito_bp.carrito_root",
        "carrito_bp.resumen",
        "catalogo.listar_catalogo",
        "categorias.obtener_categorias",
        "conversations_bp.get_conversation_timeline",
        "conversations_bp.get_link_request_status",
        "education.get_education_operations_heatmap",
        "education.get_education_operations_summary",
        "education.get_family_context",
        "education.get_school_case_detail",
        "education.list_school_cases",
        "encuestas_analytics_admin_bp.export_pdf_view",
        "encuestas_analytics_admin_bp.export_view",
        "encuestas_analytics_admin_bp.brief",
        "encuestas_analytics_admin_bp.dashboard",
        "encuestas_analytics_admin_bp.dashboard_tablero",
        "encuestas_analytics_bp.export_pdf_view",
        "encuestas_analytics_bp.export_view",
        "encuestas_analytics_bp.brief",
        "encuestas_analytics_bp.dashboard",
        "encuestas_analytics_bp.dashboard_tablero",
        "encuestas_analytics_legacy_bp.export_pdf_view",
        "encuestas_analytics_legacy_bp.export_view",
        "encuestas_analytics_legacy_bp.brief",
        "encuestas_analytics_legacy_bp.dashboard",
        "encuestas_analytics_legacy_bp.dashboard_tablero",
        "encuestas_analytics_municipal_bp.export_pdf_view",
        "encuestas_analytics_municipal_bp.export_view",
        "encuestas_analytics_municipal_bp.brief",
        "encuestas_analytics_municipal_bp.dashboard",
        "encuestas_analytics_municipal_bp.dashboard_tablero",
        "integracion_widget_settings.public_widget_settings",
        "kits_bp.listar",
        "municipal_legacy.municipal_categorias",
        "municipal_legacy.municipal_tickets_categorias",
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
        "public_resolver_bp.widget_config",
        "public_tenant_bp.download_catalog",
        "public_tenant_bp.get_catalog",
        "pwa_public.public_cart_summary",
        "pwa_public.public_catalog",
        "puntos_bp.historial",
        "puntos_bp.movimientos",
        "puntos_bp.saldo",
        "puntos_public_bp.historial_public",
        "puntos_public_bp.movimientos_public",
        "puntos_public_bp.saldo_public",
        "rewards_rules_bp.get_rules",
        "super_admin.list_tenants",
        "v2_saas.production_smoke_v2",
        "v2_saas.whatsapp_provider_status_v2",
        "v2_surveys.survey_analytics_v2",
        "v2_saas.whatsapp_tech_provider_sender_status_v2",
        "v2_analytics.operations_executive_summary_v2",
        "v2_tickets.ticket_ai_enrichment_v2",
        "v2_tickets.list_tickets_v2",
        "whatsapp_rules_bp.get_rules",
        "widget_public_config.obtener_config_publica",
        "widget_settings.manage_settings",
        "api_aliases.analytics_identity_coverage_alias",
        "api_aliases.carrito_alias_root",
        "api_aliases.carrito_alias_with_slug",
        "api_aliases.productos_alias",
        "api_aliases.productos_alias_with_slug",
        "public_aliases.root_carrito_alias_with_slug",
        "public_aliases.root_productos_alias_with_slug",
        "public_aliases.root_public_tenant_catalog",
    }

    missing = expected.difference(client.application.view_functions)
    assert missing == set()
    assert all(
        is_cutover_writer_view(client.application.view_functions[endpoint])
        for endpoint in expected
    )


def test_compatibility_aliases_cannot_bypass_mutating_get_fence(client):
    previous = _set_fence(client, True)
    try:
        paths = (
            "/api/analytics/identity/coverage",
            "/api/productos?tenant_slug=junin",
            "/api/junin/productos",
            "/api/carrito?tenant_slug=junin",
            "/api/junin/carrito",
            "/public/tenants/junin/catalog",
            "/junin/productos",
            "/junin/carrito",
            "/api/v2/tickets/1/ai-enrichment?tenant_slug=junin",
            "/api/v2/analytics/operations/executive-summary?tenant_slug=junin",
            "/admin/ai/provider-status?smoke=1&live=1",
            "/categorias",
            "/municipal/categorias",
            "/municipal/tickets/categorias",
            "/api/admin/tenants",
            "/api/admin/tenants/junin/tickets/unread-summary",
            "/api/admin/tenants/junin/dashboard-bundle",
            "/api/admin/tenants/junin/integrations/mercadolibre/preview",
            "/api/v2/integrations/whatsapp/status",
            "/api/public/widget-config?tenant_slug=junin",
            "/integracion/widget-settings?tenant_slug=junin",
            "/admin/tickets/1/ai-enrichment",
            "/api/v2/surveys/1/analytics?tenant_slug=junin",
            "/api/v2/production-smoke",
            "/api/encuestas/1/analytics/brief",
            "/api/encuestas/1/analytics/dashboard",
            "/api/encuestas/1/analytics/tablero",
            "/api/pwa/kits",
            "/api/puntos/saldo",
            "/puntos/historial",
            "/api/puntos/movimientos",
            "/api/rewards/rules",
        )
        for path in paths:
            response = client.get(path)
            assert response.status_code == 503, path
            assert response.get_json()["reason_code"] == "cutover_writer_fence_enabled"
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_tenant_resolver_never_materializes_anonymous_user_while_fenced(client):
    anon_id = "cutover-fence-anon-user"
    previous = _set_fence(client, True)
    try:
        with client.application.test_request_context("/"):
            assert User.query.filter_by(anon_id=anon_id).first() is None
            resolved = tenant_resolver._build_anon_user(anon_id)
            assert resolved.id is None
            assert User.query.filter_by(anon_id=anon_id).first() is None
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_tenant_resolver_never_lazy_creates_demo_tenant_while_fenced(client):
    previous = _set_fence(client, True)
    try:
        with client.application.test_request_context("/"):
            assert tenant_resolver._get_or_create_demo_tenant(
                "cutover-demo-that-does-not-exist"
            ) is None
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous


def test_disabled_cutover_writer_fence_does_not_replace_normal_routing(client):
    previous = _set_fence(client, False)
    try:
        assert client.post("/api/cutover-probe").status_code == 404
    finally:
        client.application.config["CUTOVER_WRITER_FENCE_ENABLED"] = previous
