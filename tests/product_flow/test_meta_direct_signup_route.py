"""Exercise the registered signup HTTP routes with local accounts and a fake Graph transport."""
import base64
import json
from types import SimpleNamespace

import pytest

from models import AuditEvent, MetaDirectSignupSession, ProviderConnection, ProviderSender, TenantProfile, User, db
from services import meta_direct_signup as signup
from services.meta_credential_operations import APP_ID
from services.meta_direct_signup_graph import DirectSignupGraph
from services.meta_whatsapp_runtime import APP_SECRETS_KEY
from services.twilio_tech_provider import STATE_KEY
from tests.test_meta_direct_signup_graph import CODE, NOW, PHONE, SECRET, TOKEN, WABA, provider


def endpoint(slug):
    return f"/api/v2/tenants/{slug}/whatsapp/tech-provider/embedded-signup"


def make_admin(label, tenant=None):
    user = User(name=label, email=f"{label}@example.invalid", rol="admin", tipo_chat="pyme")
    user.set_password("route-test-password")
    db.session.add(user)
    db.session.flush()
    if tenant is None:
        tenant = TenantProfile(slug=label, nombre=label, tipo="pyme", plan="full", pyme_id=user.id)
        db.session.add(tenant)
        db.session.flush()
    user.tenant_slug, user.tenant_id = tenant.slug, tenant.id
    db.session.commit()
    return user, tenant


def login(app, user):
    browser = app.test_client()
    response = browser.post("/auth/login", json={"email": user.email, "password": "route-test-password"})
    assert response.status_code == 200, response.get_json()
    return browser


def finish_payload(state, **extensions):
    return {
        "sessionInfo": {"type": "WA_EMBEDDED_SIGNUP", "event": "FINISH", "data": {
            "waba_id": WABA, "phone_number_id": PHONE, "session_id": "synthetic-sdk-session",
        }},
        "authResponse": {"code": CODE},
        "signup_state": state,
        **extensions,
    }


@pytest.fixture
def flow(app, client, monkeypatch, provider):
    # The main Flask fixture owns the in-memory DB. Each operator logs in using
    # a fresh browser so re-used panel cookies cannot mask an identity change.
    settings = {
        signup.FLAG: True,
        "SECRET_KEY": "route-tests-with-an-isolated-signing-key",
        "TENANT_PROVIDER_CREDENTIAL_KEYRING": json.dumps({"signup": base64.b64encode(b"x" * 32).decode()}),
        "TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID": "signup",
        APP_SECRETS_KEY: {APP_ID: SECRET},
    }
    for key, value in settings.items():
        monkeypatch.setitem(app.config, key, value)
    monkeypatch.setattr(signup, "time", SimpleNamespace(time=lambda: NOW))
    monkeypatch.setattr(DirectSignupGraph, "_call", provider.graph._call)
    user, tenant = make_admin("signup-route-primary")
    return SimpleNamespace(app=app, browser=login(app, user), tenant=tenant,
                           url=endpoint(tenant.slug), provider=provider)


def begin(flow):
    response = flow.browser.post(flow.url + "/sessions", json={})
    assert response.status_code == 201, response.get_json()
    return response.get_json()["signup_state"]


def test_panel_discovers_direct_mode_and_only_the_matching_login_configuration(flow, monkeypatch):
    url = flow.url.removesuffix("/embedded-signup")
    monkeypatch.setitem(flow.app.config, "TWILIO_META_APP_ID", APP_ID)
    monkeypatch.setitem(flow.app.config, "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID", "112233")

    def contract():
        result = flow.browser.get(url)
        assert result.status_code == 200
        return result.get_json()

    capability = contract()["direct_embedded_signup"]
    labels = capability.pop("labels")
    assert capability == {"enabled": True, "app_id": APP_ID, "configuration_id": "112233"}
    assert labels["manage"] == "Administrar conexión con Meta"
    assert all(isinstance(text, str) and text.strip() for text in labels.values())
    monkeypatch.setitem(flow.app.config, "TWILIO_META_APP_ID", "556677")
    assert contract()["direct_embedded_signup"]["configuration_id"] is None
    monkeypatch.setitem(flow.app.config, signup.FLAG, False)
    assert "direct_embedded_signup" not in contract()
    assert not flow.provider.calls


def test_reserved_organization_keeps_its_existing_signup_with_feature_on(flow, monkeypatch):
    from dataclasses import replace
    protected = signup.TARGETS["junin-production"]
    monkeypatch.setitem(signup.TARGETS, protected.name, replace(protected, tenant_id=flow.tenant.id))
    contract = flow.browser.get(flow.url.removesuffix("/embedded-signup"))
    assert contract.status_code == 200 and "direct_embedded_signup" not in contract.get_json()
    result = flow.browser.post(flow.url, json=finish_payload(None))
    assert result.status_code == 200
    assert result.get_json()["contract_version"] == "twilio.tech_provider.embedded_signup.v1"
    assert not flow.provider.calls and MetaDirectSignupSession.query.count() == 0


def test_signup_binds_only_after_verified_subscription_and_replay_is_idempotent(flow):
    state = begin(flow)
    assert MetaDirectSignupSession.query.one().status == "issued"
    assert not flow.provider.calls
    payload = finish_payload(state)
    done = flow.browser.post(flow.url, json=payload)
    assert done.status_code == 200, done.get_json()
    assert done.get_json()["status"] == "connected"
    assert "no-store" in done.headers["Cache-Control"]
    row = MetaDirectSignupSession.query.one()
    connection = db.session.get(ProviderConnection, row.connection_id)
    sender = db.session.get(ProviderSender, row.sender_id)
    assert (row.status, connection.status, sender.status) == ("connected",) * 3
    assert row.tenant_id == connection.tenant_id == sender.tenant_id == flow.tenant.id
    assert sender.provider_connection_id == connection.id
    assert AuditEvent.query.filter_by(tenant_id=flow.tenant.id, event_type="whatsapp.meta_signup_connected").count() == 1
    assert all(value not in done.get_data(as_text=True) for value in (CODE, TOKEN, state))
    assert TOKEN not in json.dumps(connection.config)
    calls = [key for key, _ in flow.provider.calls]
    assert calls.count(("POST", "oauth/access_token")) == 1
    assert calls[-2:] == [("POST", WABA + "/subscribed_apps"), ("GET", WABA + "/subscribed_apps")]
    replay = flow.browser.post(flow.url, json=payload)
    assert replay.status_code == 200 and replay.get_json() == done.get_json()
    assert len(flow.provider.calls) == len(calls)


def test_disabled_feature_keeps_legacy_completion_on_existing_database(flow, monkeypatch):
    state = begin(flow)
    monkeypatch.setitem(flow.app.config, signup.FLAG, False)
    MetaDirectSignupSession.__table__.drop(db.engine)
    assert flow.browser.post(flow.url + "/sessions", json={}).status_code == 404
    assert flow.browser.post(flow.url + "/resume", json={"signup_state": state}).status_code == 404
    response = flow.browser.post(flow.url, json=finish_payload(state))
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["contract_version"] == "twilio.tech_provider.embedded_signup.v1"
    assert result["persisted"] is True and result["provider_calls_performed"] is False
    assert (db.session.get(TenantProfile, flow.tenant.id).configuracion or {})[STATE_KEY]["embedded_signup_code_present"]
    assert not flow.provider.calls


def test_resume_checks_pending_subscription_without_the_authorization_code(flow):
    state = begin(flow)
    readback = ("GET", WABA + "/subscribed_apps")
    subscribed = flow.provider.replies[readback]
    flow.provider.replies[readback] = {"data": []}
    callback = flow.browser.post(flow.url, json=finish_payload(state))
    assert callback.status_code == 202, callback.get_json()
    assert callback.get_json()["status"] == "pending"
    row = MetaDirectSignupSession.query.one()
    assert db.session.get(ProviderConnection, row.connection_id).status == "pending"
    assert not AuditEvent.query.filter_by(tenant_id=flow.tenant.id, event_type="whatsapp.meta_signup_connected").count()

    flow.provider.replies[readback] = subscribed
    resume = flow.browser.post(flow.url + "/resume", json={"signup_state": state})
    assert resume.status_code == 200, resume.get_json()
    assert resume.get_json()["status"] == "connected"
    assert [key for key, _ in flow.provider.calls].count(("POST", "oauth/access_token")) == 1
    assert ProviderConnection.query.filter_by(tenant_id=flow.tenant.id, status="connected").count() == 1
    assert ProviderSender.query.filter_by(tenant_id=flow.tenant.id, status="connected").count() == 1


def test_missing_migration_stops_before_graph_exchange(flow):
    state = begin(flow)
    MetaDirectSignupSession.__table__.drop(db.engine)
    for url, payload in ((flow.url + "/sessions", {}), (flow.url, finish_payload(state))):
        response = flow.browser.post(url, json=payload)
        assert response.status_code == 503, response.get_json()
        assert response.get_json()["ok"] is False
    assert not flow.provider.calls
    assert not ProviderConnection.query.filter_by(tenant_id=flow.tenant.id).count()


def test_state_is_bound_to_original_organization_and_actor(flow):
    state = begin(flow)
    colleague, _ = make_admin("signup-route-colleague", flow.tenant)
    outsider, other = make_admin("signup-route-other")
    for user, url in ((colleague, flow.url), (outsider, endpoint(other.slug)), (outsider, flow.url)):
        response = login(flow.app, user).post(url, json=finish_payload(state))
        assert response.status_code == 403, response.get_json()
    assert MetaDirectSignupSession.query.one().status == "issued"
    assert not flow.provider.calls


def test_validate_only_does_not_consume_the_issued_state_or_contact_graph(flow):
    state = begin(flow)
    response = flow.browser.post(flow.url, json=finish_payload(state, validate_only=True))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["persisted"] is False
    assert response.get_json()["provider_calls_performed"] is False
    row = MetaDirectSignupSession.query.one()
    assert (row.status, row.code_hash, row.provider_session_hash) == ("issued", None, None)
    assert not ProviderConnection.query.filter_by(tenant_id=flow.tenant.id).count()
    assert not flow.provider.calls
