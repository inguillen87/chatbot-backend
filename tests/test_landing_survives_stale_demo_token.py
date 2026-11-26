"""A leftover demo token must not break the platform's own landing or widget."""

from unittest.mock import patch

import pytest
from flask import Flask

from routes.public_resolver import public_resolver_bp
from services.tenant_resolver import TenantResolutionError


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(public_resolver_bp)
    # Any token in these tests is one that no longer resolves.
    with patch(
        "routes.public_resolver.resolve_tenant_only",
        side_effect=TenantResolutionError("Tenant no encontrado para la identidad solicitada"),
    ), patch("routes.public_resolver._try_get_demo_tenant", return_value=None):
        yield app.test_client()


PLATFORM = {"Host": "www.chatboc.ar"}


def test_landing_experience_ignores_a_token_that_no_longer_resolves(client):
    stale = dict(PLATFORM, **{"X-Entity-Token": "token-de-una-demo-vencida"})
    response = client.get("/api/public/landing-experience", headers=stale)

    assert response.status_code == 200
    assert "hero" in response.get_json()


def test_landing_experience_ignores_a_stale_cookie(client):
    client.set_cookie("widget_token", "token-de-una-demo-vencida", domain="www.chatboc.ar")
    response = client.get("/api/public/landing-experience", headers=PLATFORM)

    assert response.status_code == 200


def test_platform_widget_config_ignores_a_token_that_no_longer_resolves(client):
    stale = dict(PLATFORM, **{"X-Entity-Token": "token-de-una-demo-vencida"})
    response = client.get("/api/public/widget-config?platform=1", headers=stale)

    assert response.status_code == 200


def test_an_explicit_unknown_organization_is_still_not_found(client):
    response = client.get(
        "/api/public/landing-experience?tenant_slug=organizacion-que-no-existe", headers=PLATFORM
    )

    assert response.status_code == 404
