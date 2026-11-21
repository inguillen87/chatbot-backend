"""Direct business connections share the renewal fence and the outbound binding contract."""
import json
from types import SimpleNamespace
import pytest

from models import AuditEvent, ProviderConnection, ProviderSender, TenantProfile, User, db
from services import meta_credential_renewal as renewal
from services import meta_whatsapp_credentials as vault
from services.meta_whatsapp_runtime import MetaOutboundRuntime
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from tests.test_meta_credential_operations import APP_SECRET, NOW, environment
from tests.test_meta_credential_renewal import DAY, SCOPES, install, run


APP = "890000000000001"


def install_direct(run, number):
    owner = User(name="Test owner", email=f"team-{number}@example.invalid", rol="admin",
                 password_hash="synthetic-not-a-login")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug=f"business-{number}", nombre="Test business", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.flush()
    waba = f"80000{number}"
    phone_id = f"90000{number}"
    connection = ProviderConnection(
        tenant_id=tenant.id, provider="meta", channel="whatsapp", environment="production",
        status="connected", external_app_id=APP, external_account_id=waba, credentials_ref=vault.VAULT_REF,
    )
    db.session.add(connection)
    db.session.flush()
    token = f"synthetic-business-token-{number}"
    connection.config = {
        renewal.DIRECT_MARKER: True,
        PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=connection, tenant_id=tenant.id, access_token=token,
            revision=1, expires_at=NOW + DAY, now=NOW, app_config=run.config,
        ),
    }
    sender = ProviderSender(tenant_id=tenant.id, provider_connection_id=connection.id, channel="whatsapp",
                            phone_number_id=phone_id, phone_number=f"+1555000{number:04d}",
                            waba_id=waba, status="connected")
    db.session.add(sender)
    db.session.commit()
    return SimpleNamespace(app_id=APP, tenant_id=tenant.id, connection_id=connection.id, phone_number_id=phone_id,
                           waba_id=waba, token=token, sender_id=sender.id)


class Graph:
    def __init__(self, accounts):
        self.accounts = accounts
        self.calls = []
        self.on_debug = None
        self.callback_override = None

    def __call__(self, method, url, *, headers, params, **kwargs):
        assert method == "GET"
        endpoint = url.rsplit("/", 1)[-1]
        if endpoint == "debug_token":
            account = next(a for a in self.accounts if a.token == params["input_token"])
            assert headers["Authorization"] == "Bearer " + APP + "|" + APP_SECRET
            if self.on_debug:
                self.on_debug()
            payload = {"is_valid": True, "app_id": APP, "expires_at": 0, "scopes": SCOPES,
                       "granular_scopes": [{"scope": scope, "target_ids": [account.waba_id]} for scope in SCOPES]}
        else:
            account = next(a for a in self.accounts if headers["Authorization"] == "Bearer " + a.token)
            assert url.endswith(f"/{account.waba_id}/{endpoint}")
            if endpoint == "phone_numbers":
                payload = [{"id": account.phone_number_id}]
            else:
                assert endpoint == "subscribed_apps"
                payload = [{"whatsapp_business_api_data": {"id": APP}}]
                if self.callback_override:
                    payload[0]["override_callback_uri"] = self.callback_override
        self.calls.append((account.tenant_id, endpoint))
        return SimpleNamespace(status_code=200, content=json.dumps({"data": payload}).encode())


def schedule(run, monkeypatch, accounts):
    graph = Graph(accounts)
    run.config["META_WHATSAPP_APP_SECRETS"] = {APP: APP_SECRET}
    monkeypatch.setattr(renewal, "native_graph_request", graph)
    monkeypatch.setattr(renewal, "time", SimpleNamespace(time=lambda: NOW))
    # The static schedule is covered separately. This fixture exercises only business accounts.
    monkeypatch.setattr(renewal, "renew_target", lambda name, **kwargs: {"target": name})
    return graph


def test_opt_in_keyset_pages_renew_each_business_and_runtime_can_load_it(run, monkeypatch):
    accounts = [install_direct(run, i) for i in (70, 71)]
    graph = schedule(run, monkeypatch, accounts)
    monkeypatch.setattr(renewal, "DIRECT_PAGE_SIZE", 1)
    assert renewal.run_renewal(run.config) == [{"target": "tdf-sandbox"}]
    assert not graph.calls

    run.config[renewal.DIRECT_FLAG] = True
    run.config[renewal.FLAG] = False
    assert renewal.run_renewal(run.config) == [{"target": "tdf-sandbox"}]
    assert not graph.calls
    run.config[renewal.FLAG] = True
    result = renewal.run_renewal(run.config)
    assert [r["state"] for r in result[1:]] == ["ok", "ok"]
    assert len(graph.calls) == 6
    assert {event.tenant_id for event in AuditEvent.query.all()} == {a.tenant_id for a in accounts}
    assert len(renewal.run_renewal(run.config)) == 3
    assert len(graph.calls) == 6
    runtime = MetaOutboundRuntime(run.config, get=graph, clock=lambda: NOW)
    for account in accounts:
        binding = runtime.load_binding(account)
        assert binding.sender.connection_id == account.connection_id
        assert binding.sender.sender_id == account.sender_id
        assert binding.credential.revision == 1 and binding.credential.expires_at == NOW + 30 * DAY
    assert len(graph.calls) == 12


def test_reserved_assets_and_unmarked_or_pending_connections_are_never_opened(run, monkeypatch):
    accounts = [install_direct(run, i) for i in range(70, 75)]
    protected = renewal.ops.TARGETS["junin-production"]
    db.session.get(ProviderConnection, accounts[0].connection_id).external_account_id = protected.waba_id
    db.session.get(ProviderSender, accounts[1].sender_id).phone_number_id = protected.phone_id
    db.session.get(ProviderSender, accounts[2].sender_id).phone_number = protected.phone
    db.session.get(ProviderConnection, accounts[3].connection_id).status = "pending"
    unmarked = db.session.get(ProviderConnection, accounts[4].connection_id)
    unmarked.config = {PRIVATE_CONFIG_KEY: unmarked.config[PRIVATE_CONFIG_KEY]}
    db.session.commit()
    graph = schedule(run, monkeypatch, accounts)
    run.config[renewal.DIRECT_FLAG] = True
    result = renewal.run_renewal(run.config)
    assert all(r.get("state") != "ok" for r in result)
    assert not graph.calls and AuditEvent.query.count() == 0


@pytest.mark.parametrize("change", ["marker", "feature_flag", "foreign_waba"])
def test_changed_binding_or_policy_during_observation_blocks_reseal(run, monkeypatch, change):
    account = install_direct(run, 70)
    graph = schedule(run, monkeypatch, [account])
    run.config[renewal.DIRECT_FLAG] = True
    before = db.session.get(ProviderConnection, account.connection_id).config[PRIVATE_CONFIG_KEY].copy()
    def withdraw():
        if change == "feature_flag":
            run.config[renewal.DIRECT_FLAG] = False
        elif change == "marker":
            connection = db.session.get(ProviderConnection, account.connection_id)
            connection.config = {PRIVATE_CONFIG_KEY: before}
            db.session.commit()
        else:
            foreign = install_direct(run, 71)
            db.session.get(ProviderConnection, foreign.connection_id).external_account_id = account.waba_id
            db.session.commit()
    graph.on_debug = withdraw
    assert renewal.run_renewal(run.config)[1]["state"] == "disabled"
    db.session.expire_all()
    config = db.session.get(ProviderConnection, account.connection_id).config
    assert config[PRIVATE_CONFIG_KEY] == before and renewal.RENEWAL_KEY not in config
    assert AuditEvent.query.count() == 0


def test_direct_subscription_with_changed_callback_never_renews_envelope(run, monkeypatch):
    account = install_direct(run, 76)
    graph = schedule(run, monkeypatch, [account])
    graph.callback_override = "https://example.invalid/alternate"
    run.config[renewal.DIRECT_FLAG] = True
    before = db.session.get(ProviderConnection, account.connection_id).config[PRIVATE_CONFIG_KEY].copy()
    result = renewal.run_renewal(run.config)[1]
    assert result["state"] == "failed" and result["reason"] == "meta_app_subscription_unverified"
    db.session.expire_all()
    record = db.session.get(ProviderConnection, account.connection_id)
    assert record.config[PRIVATE_CONFIG_KEY] == before
    assert AuditEvent.query.one().event_type == "provider_credential.meta_renewal_failed"


def test_existing_sandbox_renewal_retains_its_custom_callback(run):
    install(run.config, NOW + DAY)
    run.graph.apps[0]["override_callback_uri"] = "https://example.invalid/sandbox-callback"
    assert run.renew()["state"] == "ok"
