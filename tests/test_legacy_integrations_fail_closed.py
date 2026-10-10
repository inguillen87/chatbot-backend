from unittest.mock import patch

import pytest
from flask import Flask

from routes.integrations import integrations_bp
from services.integrations.mercadolibre_service import MercadoLibreService
from services.integrations.tiendanube_service import TiendaNubeService


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        LEGACY_INTEGRATIONS_TRANSPORT_ENABLED=False,
    )
    app.register_blueprint(integrations_bp, url_prefix="/api/integrations")
    return app.test_client()


def _assert_default_denial(response, reason_code):
    assert response.status_code == 404
    payload = response.get_json()
    assert payload["contract_version"] == "tenant.integration.legacy_transport_disabled.v1"
    assert payload["status"] == "disabled"
    assert payload["reason_code"] == reason_code
    assert payload["retryable"] is False
    assert "tenant" not in payload
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("query_tenant_id", ["101", "202"])
def test_unsigned_webhook_rejects_request_tenant_without_parsing_or_mutation(
    client,
    query_tenant_id,
):
    with patch(
        "services.integrations.mercadolibre_service.MercadoLibreService.process_webhook"
    ) as process_webhook, patch("routes.integrations.TenantProfile") as tenant_model:
        response = client.post(
            f"/api/integrations/webhooks/mercadolibre?tenant_id={query_tenant_id}",
            json={
                "tenant_id": "999",
                "event_id": f"cross-tenant-{query_tenant_id}",
                "resource": "/orders/unsafe",
            },
        )

    _assert_default_denial(response, "legacy_unsigned_webhook_disabled")
    assert response.get_json()["replacement_endpoint"] == (
        "/api/omnichannel/adapters/{connection_id}/inbound"
    )
    process_webhook.assert_not_called()
    assert not tenant_model.query.get.called


@pytest.mark.parametrize("state_tenant_id", ["101", "202"])
def test_oauth_callback_rejects_plain_cross_tenant_state_without_lookup_or_exchange(
    client,
    state_tenant_id,
):
    with patch(
        "services.integrations.mercadolibre_service.MercadoLibreService.handle_callback"
    ) as handle_callback, patch("routes.integrations.TenantProfile") as tenant_model:
        response = client.get(
            "/api/integrations/mercadolibre/callback",
            query_string={"code": "provider-code", "state": state_tenant_id},
        )

    _assert_default_denial(response, "legacy_oauth_callback_disabled")
    handle_callback.assert_not_called()
    assert not tenant_model.query.get.called


def test_oauth_connect_cannot_generate_plain_tenant_state(client):
    with patch(
        "services.integrations.mercadolibre_service.MercadoLibreService.get_auth_url"
    ) as get_auth_url, patch("routes.integrations.TenantProfile") as tenant_model:
        response = client.post(
            "/api/integrations/mercadolibre/connect",
            json={"tenant_id": 202},
            headers={"X-Tenant": "tenant-101"},
        )

    _assert_default_denial(response, "legacy_oauth_connect_disabled")
    assert "url" not in response.get_json()
    get_auth_url.assert_not_called()
    assert not tenant_model.query.get.called


def test_mistaken_flag_enablement_still_cannot_revive_unsafe_transport(client):
    app = client.application
    app.config["LEGACY_INTEGRATIONS_TRANSPORT_ENABLED"] = True

    with patch(
        "services.integrations.mercadolibre_service.MercadoLibreService.process_webhook"
    ) as process_webhook:
        response = client.post(
            "/api/integrations/webhooks/mercadolibre?tenant_id=202",
            json={"tenant_id": 101, "event_id": "must-not-run"},
        )

    assert response.status_code == 503
    payload = response.get_json()
    assert payload["reason_code"] == "legacy_integration_secure_transport_unavailable"
    assert payload["retryable"] is False
    process_webhook.assert_not_called()


def test_whatsapp_deprecated_callback_stays_side_effect_free(client):
    with patch("routes.integrations.TenantProfile") as tenant_model:
        response = client.get(
            "/api/integrations/whatsapp/callback",
            query_string={"code": "ignored", "state": "202"},
        )

    assert response.status_code == 410
    assert response.get_json()["reason_code"] == "use_twilio_tech_provider_flow"
    assert not tenant_model.query.get.called


@pytest.mark.parametrize(
    ("callable_under_test", "args"),
    [
        (MercadoLibreService.get_auth_url, (101, "https://callback.invalid")),
        (MercadoLibreService.handle_callback, (101, "code", "https://callback.invalid")),
        (MercadoLibreService.process_webhook, ({"topic": "orders"}, 101)),
        (TiendaNubeService.get_auth_url, (101, "https://callback.invalid")),
        (TiendaNubeService.handle_callback, (101, "code", "https://callback.invalid")),
    ],
)
def test_legacy_service_entry_points_also_fail_closed(callable_under_test, args):
    with pytest.raises(RuntimeError, match="^legacy_integration_transport_disabled$"):
        callable_under_test(*args)
