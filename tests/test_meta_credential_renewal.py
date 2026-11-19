"""Offline renewal against synthetic Graph responses and an actual local database."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import logging
from types import SimpleNamespace

import pytest

from models import AuditEvent, ProviderConnection, db
from services import meta_credential_admin_runtime as admin
from services import meta_credential_operations as ops
from services import meta_credential_renewal as renewal
from services import meta_whatsapp_credentials as vault
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from tests.test_meta_credential_admin import body, http, post
from tests.test_meta_credential_operations import APP_SECRET, KEY, NOW, OLD_TOKEN, TOKEN, environment

DAY = 86400
TARGET = ops.TARGETS["tdf-sandbox"]
SCOPES = ["whatsapp_business_management", "whatsapp_business_messaging"]
RENEWAL_FIELDS = {"mode", "state", "last_revalidated_at", "next_check_after", "reason"}
SUCCESS = "provider_credential.meta_revalidated"
FAILURE = "provider_credential.meta_renewal_failed"


def install(config, expiry, *, revision=6, token=TOKEN):
    connection = db.session.get(ProviderConnection, TARGET.connection_id)
    envelope = vault.seal_token(connection=connection, tenant_id=TARGET.tenant_id,
        access_token=token, revision=revision, expires_at=expiry, now=expiry - 30 * DAY, app_config=config)
    connection.config = {**connection.config, PRIVATE_CONFIG_KEY: envelope}
    db.session.commit()
    db.session.remove()
    return envelope


def connection_config():
    db.session.expire_all()
    return deepcopy(db.session.get(ProviderConnection, TARGET.connection_id).config)


class FakeGraph:
    def __init__(self):
        self.debug = {"is_valid": True, "app_id": ops.APP_ID, "expires_at": 0,
            "data_access_expires_at": 0, "scopes": list(SCOPES),
            "granular_scopes": [{"scope": s, "target_ids": [TARGET.waba_id]} for s in SCOPES]}
        self.phones = [{"id": TARGET.phone_id, "display_phone_number": TARGET.phone}]
        self.apps = [{"whatsapp_business_api_data": {"id": ops.APP_ID}}]
        self.calls = []
        self.status = 200
        self.once = None
        self.overrides = {}

    def __call__(self, method, url, **kwargs):
        assert method == "GET" and kwargs["verify"] is True and kwargs["allow_redirects"] is False
        endpoint = url.rsplit("/", 1)[-1]
        assert endpoint in {"debug_token", "phone_numbers", "subscribed_apps"}
        self.calls.append(endpoint)
        if self.once is not None:
            hook, self.once = self.once, None
            hook()
        if endpoint == "debug_token":
            assert kwargs["params"]["input_token"] == TOKEN
            assert kwargs["headers"]["Authorization"] == "Bearer " + ops.APP_ID + "|" + APP_SECRET
            data = self.debug
        else:
            assert url.endswith("/" + TARGET.waba_id + "/" + endpoint)
            assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
            data = self.phones if endpoint == "phone_numbers" else self.apps
        status, payload = self.overrides.get(endpoint, (self.status, {"data": data}))
        return SimpleNamespace(status_code=status, content=json.dumps(payload).encode())


@pytest.fixture
def run(environment, monkeypatch):
    config = environment.app.config
    config["VERCEL_META_CREDENTIAL_RENEWAL_CRON_ENABLED"] = True
    graph = FakeGraph()
    decision = SimpleNamespace(allowed=True, enabled=True, epoch=6)
    state = SimpleNamespace(now=NOW, config=config, graph=graph, lease_decision=decision)
    monkeypatch.setattr(renewal, "evaluate_global_writer_authority", lambda cfg: decision)

    @contextmanager
    def lease(config):
        try:
            yield SimpleNamespace(revalidate=lambda cfg: state.lease_decision)
        except Exception:
            # The background authority connection sanitizes exceptions that
            # escape its context, unlike the request-owned middleware lease.
            raise RuntimeError("authority_connection_unavailable") from None

    monkeypatch.setattr(renewal, "global_writer_authority_lease", lease)
    state.renew = lambda target=TARGET.name: renewal.renew_target(target,
        config=config, graph_get=graph, clock=lambda: state.now)
    return state


def assert_secrets_absent(values, *envelopes):
    # A failing privacy check must not include the secret in pytest's explanation.
    secret_values = [TOKEN, OLD_TOKEN, APP_SECRET, KEY, hashlib.sha256(TOKEN.encode()).hexdigest(),
                     '"ciphertext"', '"nonce"']
    for envelope in envelopes:
        secret_values.extend((envelope["ciphertext"], envelope["nonce"]))
    if any(value in values for value in secret_values):
        pytest.fail("Sensitive credential data reached the observable output")


@pytest.mark.parametrize("revision", [6, 7])
def test_renew_at_window_boundary_preserves_token_revision_and_audits_once(run, revision, caplog):
    old = install(run.config, NOW + 10*DAY, revision=revision)
    with caplog.at_level(logging.DEBUG):
        result = run.renew()
        repeated = run.renew()
    assert result["state"] == repeated["state"] == "ok"
    assert run.graph.calls == ["debug_token", "phone_numbers", "subscribed_apps"]
    cfg = connection_config()
    sealed = cfg[PRIVATE_CONFIG_KEY]
    assert sealed["revision"] == revision and sealed["expires_at"] == NOW + 30*DAY
    assert sealed["nonce"] != old["nonce"] and sealed["ciphertext"] != old["ciphertext"]
    connection = db.session.get(ProviderConnection, TARGET.connection_id)
    assert vault.open_token(connection=connection, tenant_id=TARGET.tenant_id, now=NOW,
                            app_config=run.config).access_token == TOKEN
    assert cfg["retain_note"] == "unchanged"
    assert set(cfg["meta_credential_renewal"]) == RENEWAL_FIELDS
    assert cfg["meta_credential_renewal"]["last_revalidated_at"] == NOW
    assert cfg["meta_credential_renewal"]["next_check_after"] == NOW + 20*DAY
    event = AuditEvent.query.one()
    assert (event.event_type, event.tenant_id, event.resource_id) == (
        SUCCESS, TARGET.tenant_id, str(TARGET.connection_id))
    assert_secrets_absent(caplog.text + json.dumps([result, repeated, event.details,
        cfg["meta_credential_renewal"]]), old, sealed)


@pytest.mark.parametrize(("provider", "data_access", "days"), [(15, 0, 15), (25, 14, 14), (3, 0, 3)])
def test_respects_the_earliest_finite_provider_deadline(run, provider, data_access, days):
    install(run.config, NOW + DAY)
    run.graph.debug.update(expires_at=NOW + provider*DAY,
                           data_access_expires_at=NOW + data_access*DAY if data_access else 0)
    run.renew()
    cfg = connection_config()
    assert cfg[PRIVATE_CONFIG_KEY]["expires_at"] == NOW + days*DAY
    assert AuditEvent.query.one().event_type == SUCCESS
    count = len(run.graph.calls)
    run.renew()
    assert len(run.graph.calls) == count and connection_config() == cfg


@pytest.mark.parametrize(("remaining", "expected"), [(10*DAY + 1, "ok"), (0, "expired"), (-1, "expired")])
def test_outside_window_and_expired_skip_provider_and_writes(run, remaining, expected):
    install(run.config, NOW + remaining)
    original = connection_config()
    assert run.renew()["state"] == expected
    assert run.graph.calls == [] and connection_config() == original and AuditEvent.query.count() == 0


def test_junin_and_disabled_flag_skip_graph_and_all_writes(run):
    install(run.config, NOW + DAY)
    original = connection_config()
    assert run.renew("junin-production")["state"] == "disabled"
    run.config["VERCEL_META_CREDENTIAL_RENEWAL_CRON_ENABLED"] = False
    assert run.renew()["state"] == "disabled"
    assert run.graph.calls == [] and connection_config() == original and AuditEvent.query.count() == 0


@pytest.mark.parametrize(("mismatch", "reason"), [
    ("revoked", "meta_token_authority_invalid"), ("app", "meta_token_authority_invalid"),
    ("scopes", "meta_token_authority_invalid"), ("waba", "meta_token_granular_scope_mismatch"),
    ("missing_granular", "meta_token_granular_scope_mismatch"),
    ("phone", "meta_phone_membership_unverified"), ("subscription", "meta_app_subscription_unverified"),
    ("provider_expired", "meta_token_authority_expired"),
])
def test_definitive_rejection_sets_state_without_extending(run, mismatch, reason, caplog):
    old = install(run.config, NOW + DAY)
    graph = run.graph
    if mismatch == "revoked":
        graph.debug["is_valid"] = False
    elif mismatch == "app":
        graph.debug["app_id"] = "777"
    elif mismatch == "scopes":
        graph.debug["scopes"] = SCOPES[:1]
    elif mismatch == "waba":
        graph.debug["granular_scopes"][1]["target_ids"] = ["777"]
    elif mismatch == "missing_granular":
        del graph.debug["granular_scopes"]
    elif mismatch == "phone":
        graph.phones = [{"id": "777"}]
    elif mismatch == "subscription":
        graph.apps = []
    else:
        graph.debug["expires_at"] = NOW
    with caplog.at_level(logging.DEBUG):
        result = run.renew()
        assert run.renew()["state"] == "failed"
    cfg = connection_config()
    assert cfg[PRIVATE_CONFIG_KEY] == old
    assert result["state"] == "failed" and result["reason"] == reason
    assert cfg["meta_credential_renewal"]["reason"] == reason
    assert cfg["meta_credential_renewal"]["state"] == "failed"
    event = AuditEvent.query.one()
    assert event.event_type == FAILURE and event.details["reason"] == reason
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors.count("meta_credential_renewal_failed") == 1
    assert_secrets_absent(caplog.text + json.dumps([result, event.details, cfg["meta_credential_renewal"]]), old)


@pytest.mark.parametrize("fault", [429, 503, "timeout"])
def test_transient_provider_error_can_retry_without_persistent_failure(run, fault, caplog):
    old = install(run.config, NOW+DAY)
    if fault == "timeout":
        def timeout():
            raise TimeoutError("Synthetic upstream exception: " + TOKEN)
        run.graph.once = timeout
    else:
        run.graph.status = fault
    with caplog.at_level(logging.DEBUG):
        transient = run.renew()
    cfg = connection_config()
    assert transient["state"] == "due" and cfg[PRIVATE_CONFIG_KEY] == old
    assert cfg.get("meta_credential_renewal", {}).get("state") != "failed" and AuditEvent.query.count() == 0
    assert_secrets_absent(caplog.text + json.dumps(transient), old)
    run.graph.status = 200
    run.now += 120
    assert run.renew()["state"] == "ok" and AuditEvent.query.one().event_type == SUCCESS


@pytest.mark.parametrize(("endpoint", "status", "payload"), [
    ("phone_numbers", 200, {"data": [], "paging": {"next": "https://example.invalid/next"}}),
    ("subscribed_apps", 200, {"data": [], "paging": {"next": "https://example.invalid/next"}}),
    ("phone_numbers", 200, {"data": {}}),
    ("subscribed_apps", 200, {"paging": "invalid"}),
    ("debug_token", 401, {"error": {"code": 190, "message": "synthetic invalid app credential"}}),
])
def test_inconclusive_membership_or_app_auth_failure_can_retry(run, endpoint, status, payload):
    old = install(run.config, NOW + DAY)
    run.graph.overrides[endpoint] = (status, payload)
    assert run.renew()["state"] == "due"
    cfg = connection_config()
    assert cfg[PRIVATE_CONFIG_KEY] == old and "meta_credential_renewal" not in cfg
    assert AuditEvent.query.count() == 0
    run.graph.overrides.clear()
    assert run.renew()["state"] == "ok" and AuditEvent.query.one().event_type == SUCCESS


def test_adapter_debug_record_and_exception_are_redacted(run, caplog):
    old = install(run.config, NOW + DAY)
    def noisy_adapter():
        try:
            raise TimeoutError("Synthetic timeout " + KEY)
        except TimeoutError:
            logging.getLogger("synthetic.graph").debug(
                "Synthetic response %s %s %s", TOKEN, APP_SECRET, old["ciphertext"], exc_info=True)
            raise
    run.graph.once = noisy_adapter
    with caplog.at_level(logging.DEBUG):
        result = run.renew()
    assert result["state"] == "due"
    records = [r for r in caplog.records if r.name == "synthetic.graph"]
    assert len(records) == 1 and records[0].exc_info is None
    assert_secrets_absent(caplog.text + repr(records[0].args) + json.dumps(result), old)


@pytest.mark.parametrize("when", ["initial_fence", "during_graph", "stale_epoch", "lost_before_commit"])
def test_writer_must_still_be_authorized_at_commit(run, monkeypatch, when):
    old = install(run.config, NOW + DAY)
    if when == "initial_fence":
        run.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    elif when == "during_graph":
        run.graph.once = lambda: run.config.update(CUTOVER_WRITER_FENCE_ENABLED=True)
    elif when == "stale_epoch":
        run.lease_decision = SimpleNamespace(allowed=True, enabled=True, epoch=7)
    else:
        valid = run.lease_decision
        decisions = iter([valid, SimpleNamespace(allowed=False, enabled=True, epoch=6)])
        @contextmanager
        def lost(config):
            yield SimpleNamespace(revalidate=lambda cfg: next(decisions))
        monkeypatch.setattr(renewal, "global_writer_authority_lease", lost)
    run.renew()
    assert connection_config()[PRIVATE_CONFIG_KEY] == old and AuditEvent.query.count() == 0
    if when == "initial_fence":
        assert not run.graph.calls


@pytest.mark.parametrize("during", ["revision", "expiry"])
@pytest.mark.parametrize("invalid_proof", [False, True])
def test_concurrent_replacement_wins_on_positive_or_negative_provider_result(run, during, invalid_proof):
    old = install(run.config, NOW + DAY)
    expected = {}
    def replace():
        expected.update(install(run.config,
            old["expires_at"] + int(during == "expiry"),
            revision=old["revision"] + int(during == "revision"), token=OLD_TOKEN))
    run.graph.once = replace
    run.graph.debug["is_valid"] = not invalid_proof
    assert run.renew()["reason"] == "credential_changed"
    cfg = connection_config()
    assert cfg[PRIVATE_CONFIG_KEY] == expected
    assert "meta_credential_renewal" not in cfg and AuditEvent.query.count() == 0


def test_expiry_during_graph_never_resurrects_envelope(run):
    old = install(run.config, NOW+1)
    run.graph.once = lambda: setattr(run, "now", NOW+1)
    assert run.renew()["state"] == "expired"
    assert connection_config()[PRIVATE_CONFIG_KEY] == old and AuditEvent.query.count() == 0


def test_status_and_plan_expose_policy_without_opening_a_secret(http, monkeypatch):
    cfg = http.app.config
    cfg["VERCEL_META_CREDENTIAL_RENEWAL_CRON_ENABLED"] = True
    install(cfg, http.now + DAY)
    status = post(http, "status", {"target": TARGET.name})
    assert status.status_code == 200
    view = status.json["renewal"]
    assert set(view) == RENEWAL_FIELDS and view["mode"] == "automatic" and view["state"] == "due"
    assert not http.calls and AuditEvent.query.count() == 0

    graph = FakeGraph()
    monkeypatch.setattr(admin, "native_graph_request", graph)
    plan = post(http, "plan", body(TARGET.name))
    assert plan.status_code == 200
    assert plan.json["expiry"]["renewal"] == "automatic" and plan.json["expiry"]["source"] == "local_policy"
    assert plan.json["expiry"]["provider_expires_at"] is None
    assert AuditEvent.query.count() == 0
    assert_secrets_absent(status.get_data(as_text=True) + plan.get_data(as_text=True))


def test_manual_replacement_releases_old_renewal_failure(http):
    http.app.config["VERCEL_META_CREDENTIAL_RENEWAL_CRON_ENABLED"] = True
    connection = db.session.get(ProviderConnection, TARGET.connection_id)
    connection.config = {**connection.config, "meta_credential_renewal": {
        "state": "failed", "reason": "meta_token_authority_invalid",
    }}
    db.session.commit()
    request = body(TARGET.name)
    planned = post(http, "plan", request)
    assert planned.status_code == 200
    request.update(expires_at=planned.json["plan"]["expires_at"],
        approved_plan_sha256=planned.json["plan_sha256"],
        expected_database_identity_sha256=planned.json["plan"]["database_identity_sha256"])
    applied = post(http, "apply", request)
    assert applied.status_code == 200 and applied.json["revision"] == 7
    result = post(http, "status", {"target": TARGET.name})
    assert result.status_code == 200 and result.json["renewal"]["state"] in {"ok", "due"}
    assert result.json["renewal"]["reason"] is None
    assert AuditEvent.query.one().event_type == "provider_credential.meta_rotated"
