from pathlib import Path

from app import create_app
from config import LOCAL_DEV_ORIGIN_PATTERN, TestingConfig


class ProductionLikeCorsConfig(TestingConfig):
    ENV = "production"
    DEBUG = False
    SECRET_KEY = "production-test-secret-key-32-chars"
    BACKEND_URL = "https://chatbot-backend-2e14.onrender.com"
    PUBLIC_ROOT_DOMAIN = "chatboc.ar"
    CORS_ALLOW_LOCAL_DEV = True
    CORS_CREDENTIALS_ALLOWED_ORIGINS = (
        "https://www.chatboc.ar",
        LOCAL_DEV_ORIGIN_PATTERN,
    )


def test_public_widget_cors_never_allows_credentials(client):
    origin = "https://customer-store.example"
    response = client.options(
        "/auth/widget-token",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,x-entity-token",
        },
    )

    assert response.status_code == 200
    assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
    assert response.headers.get("Access-Control-Allow-Credentials") is None
    assert "Authorization" in response.headers.get("Access-Control-Allow-Headers", "")


def test_public_survey_cors_never_allows_credentials(client):
    origin = "https://participacion.example"
    response = client.get("/public/encuestas", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_operational_api_rejects_arbitrary_vercel_preview(client):
    response = client.options(
        "/api/analytics/kpis?tenant_id=1",
        headers={
            "Origin": "https://untrusted-preview.vercel.app",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_operational_api_allows_exact_production_origin(client):
    origin = "https://www.chatboc.ar"
    response = client.options(
        "/api/analytics/kpis?tenant_id=1",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
    assert response.headers.get("Access-Control-Allow-Credentials") == "true"


def test_protected_pwa_and_widget_admin_routes_use_credentialed_cors(client):
    origin = "https://www.chatboc.ar"
    for path in ("/api/pwa/app/me/tenants", "/widget-settings"):
        response = client.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
        assert response.headers.get("Access-Control-Allow-Credentials") == "true"


def test_public_pwa_routes_remain_cross_origin_without_credentials(client):
    origin = "https://citizen-portal.example"
    for path in (
        "/api/pwa/anon-id",
        "/api/pwa/tenant-info",
        "/api/pwa/public/junin/catalog",
        "/api/pwa/kits/junin",
    ):
        response = client.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )

        assert response.headers.getlist("Access-Control-Allow-Origin") == [origin]
        assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_protected_pwa_does_not_offer_credentials_to_cross_site_origins(client):
    response = client.options(
        "/api/pwa/app/me/tenants",
        headers={
            "Origin": "https://customer-store.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_production_app_drops_local_pattern_even_if_config_attempts_to_enable_it():
    production_app = create_app(ProductionLikeCorsConfig)
    with production_app.test_client() as production_client:
        rejected = production_client.options(
            "/api/analytics/kpis?tenant_id=1",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        accepted = production_client.options(
            "/api/analytics/kpis?tenant_id=1",
            headers={
                "Origin": "https://www.chatboc.ar",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert rejected.headers.get("Access-Control-Allow-Origin") is None
    assert rejected.headers.get("Access-Control-Allow-Credentials") is None
    assert accepted.headers.get("Access-Control-Allow-Origin") == "https://www.chatboc.ar"
    assert accepted.headers.get("Access-Control-Allow-Credentials") == "true"


def test_render_hostname_keeps_cors_on_ticket_crm_preflights():
    origin = "https://www.chatboc.ar"
    production_app = create_app(ProductionLikeCorsConfig)
    with production_app.test_client() as production_client:
        for path, method in (
            ("/api/admin/tenants/junin/ticket-categories", "GET"),
            ("/admin/tickets/400/ai-enrichment", "POST"),
        ):
            response = production_client.options(
                path,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": (
                        "authorization,content-type,x-tenant,x-tenant-slug"
                    ),
                },
            )

            assert response.status_code == 200
            assert response.headers.get("Access-Control-Allow-Origin") == origin
            assert response.headers.get("Access-Control-Allow-Credentials") == "true"
            assert "X-Tenant-Slug" in response.headers.get(
                "Access-Control-Allow-Headers", ""
            )


def test_render_blueprint_declares_production_edge_guards():
    blueprint = (Path(__file__).parents[1] / "render.yaml").read_text(encoding="utf-8")

    assert "healthCheckPath: /health" in blueprint
    assert "Keep its onrender.com subdomain disabled" in blueprint
    assert "value: production" in blueprint
    assert 'value: "https://chatboc.ar,https://www.chatboc.ar"' in blueprint
    assert 'value: "false"' in blueprint
