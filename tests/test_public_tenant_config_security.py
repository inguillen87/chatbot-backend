import json
from unittest.mock import patch

from database import db
from models import TenantProfile, User
from services.public_tenant_config import sanitize_public_tenant_config


def _private_config() -> dict:
    return {
        "widget_tokens": ["stored-widget-secret"],
        "mercadopago_access_token": "mp-private-token",
        "twilio_tech_provider": {
            "twilio_account_sid": "AC-private-account",
            "auth_token": "twilio-private-token",
        },
        "channels": {
            "whatsapp": {
                "enabled": True,
                "phone": "+5491112345678",
                "provider_credentials": {"client_secret": "nested-private-secret"},
            }
        },
        "copy": {"welcome": "Public welcome"},
        "auth": {
            "provider": "clerk",
            "publishable_key": "pk_live_public",
            "client_secret": "clerk-private-secret",
        },
        "items": [{"label": "Public item", "refresh_token": "nested-refresh-token"}],
        "legacyProvider": {
            "apiKey": "camel-case-api-key",
            "accountSid": "camel-case-account-sid",
            "publicLabel": "Public provider label",
        },
    }


def _create_tenant() -> TenantProfile:
    owner = User(
        name="Public Config Owner",
        email="public-config-owner@chatboc.test",
        rol="admin",
        tipo_chat="pyme",
        token="canonical-public-widget-token",
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="public-config-security",
        nombre="Public Config Security",
        tipo="pyme",
        plan="full",
        pyme_id=owner.id,
        configuracion=_private_config(),
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.commit()
    return tenant


def _assert_public_config(config: dict) -> None:
    serialized = json.dumps(config, sort_keys=True)
    for private_value in (
        "stored-widget-secret",
        "mp-private-token",
        "AC-private-account",
        "twilio-private-token",
        "nested-private-secret",
        "clerk-private-secret",
        "nested-refresh-token",
        "camel-case-api-key",
        "camel-case-account-sid",
    ):
        assert private_value not in serialized

    assert config["channels"]["whatsapp"]["phone"] == "+5491112345678"
    assert config["copy"]["welcome"] == "Public welcome"
    assert config["auth"] == {"provider": "clerk", "publishable_key": "pk_live_public"}
    assert config["items"] == [{"label": "Public item"}]
    assert config["legacyProvider"] == {"publicLabel": "Public provider label"}


def test_public_config_sanitizer_is_recursive_and_does_not_mutate_source():
    source = _private_config()

    sanitized = sanitize_public_tenant_config(source)

    _assert_public_config(sanitized)
    assert source["widget_tokens"] == ["stored-widget-secret"]
    assert source["twilio_tech_provider"]["auth_token"] == "twilio-private-token"
    assert source["channels"]["whatsapp"]["provider_credentials"]["client_secret"] == "nested-private-secret"


def test_unauthenticated_tenant_profiles_do_not_expose_private_config(client):
    tenant = _create_tenant()

    v2_response = client.get(f"/api/v2/tenants/{tenant.slug}/profile")
    legacy_response = client.get(f"/api/public/tenant-profile?tenant={tenant.slug}")

    assert v2_response.status_code == 200
    assert legacy_response.status_code == 200
    _assert_public_config(v2_response.get_json()["config"])
    _assert_public_config(legacy_response.get_json()["tenant"]["config"])


def test_public_widget_request_logs_only_a_non_reversible_token_reference(client, app):
    tenant = _create_tenant()
    token = "canonical-public-widget-token"

    with patch.object(app.logger, "info") as logger_info:
        response = client.get(
            f"/api/public/tenant-profile?tenant={tenant.slug}&widget_token={token}"
        )

    assert response.status_code == 200
    rendered_calls = repr(logger_info.call_args_list)
    assert token not in rendered_calls
    assert "entity_token_ref=%s" in rendered_calls
    assert "present:" in rendered_calls
    assert tenant.slug in rendered_calls
