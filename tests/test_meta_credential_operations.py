"""Offline real persistence/CAS/rollback with synthetic secrets and Graph GETs.

SQLite checks application transactions, not PostgreSQL locking behavior. The CLI
rejects SQLite; PostgreSQL row/table locks require a separate disposable review.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import io
import json
import logging
import os
import secrets
import tempfile
from types import SimpleNamespace

from flask import Flask
import pytest

from models import AuditEvent, ProviderConnection, ProviderSender, TenantProfile, User, WhatsappNumero, db
from services import meta_credential_operations as ops
from services import meta_whatsapp_credentials as vault
from services.tenant_provider_credentials import PRIVATE_CONFIG_KEY
from scripts import manage_meta_whatsapp_credentials as cli

NOW = 1794523200
TOKEN = secrets.token_urlsafe(48)
OLD_TOKEN = secrets.token_urlsafe(48)
APP_SECRET = secrets.token_hex(32)
KEY = base64.b64encode(secrets.token_bytes(32)).decode()


@pytest.fixture
def environment():
    app = Flask("meta-credential-ops-offline")
    app.config.update(TESTING=True, SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False, CUTOVER_WRITER_FENCE_ENABLED=False,
        TENANT_PROVIDER_CREDENTIAL_KEYRING=json.dumps({"synthetic": KEY}),
        TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID="synthetic",
        META_WHATSAPP_APP_SECRETS={ops.APP_ID: APP_SECRET}, META_WHATSAPP_GRAPH_VERSION="v25.0",
        META_TDF_SANDBOX_APP_ID=ops.APP_ID, META_TDF_SANDBOX_APP_SECRET=APP_SECRET,
        META_TDF_SANDBOX_GRAPH_VERSION="v25.0")
    db.init_app(app)
    with app.app_context():
        assert db.engine.dialect.name == "sqlite" and db.engine.url.database == ":memory:"
        db.create_all()
        actor = User(id=1, name="Synthetic operator", email="operator@example.invalid",
                     rol="super_admin", password_hash="synthetic-not-a-login")
        owner = User(id=2, name="Synthetic owner", email="owner@example.invalid",
                     rol="admin", password_hash="synthetic-not-a-login")
        db.session.add_all([actor, owner])
        db.session.flush()
        db.session.add_all([TenantProfile(id=22, slug="junin", nombre="Junín", tipo="municipio", municipio_id=2, is_active=True),
                            TenantProfile(id=46, slug="tierra-del-fuego", nombre="TDF", tipo="municipio", municipio_id=2, is_active=True)])
        db.session.flush()
        legacy = ProviderConnection(id=1, tenant_id=22, provider="twilio", channel="whatsapp",
            environment="sandbox", status="provisioning_plan_ready", credentials_ref="env:legacy",
            config={"historical": "preserve"})
        tdf = ProviderConnection(id=3, tenant_id=46, provider="meta", channel="whatsapp", environment="sandbox",
            status="connected", external_app_id=ops.APP_ID, external_account_id=ops.TARGETS["tdf-sandbox"].waba_id,
            credentials_ref=vault.VAULT_REF, config={})
        db.session.add_all([legacy, tdf])
        db.session.flush()
        tdf.config = {"retain_note": "unchanged", PRIVATE_CONFIG_KEY: vault.seal_token(connection=tdf, tenant_id=46,
            access_token=OLD_TOKEN, revision=6, expires_at=NOW - 10, now=NOW - 100,
            app_config=app.config)}
        db.session.add(ProviderSender(id=1, tenant_id=46, provider_connection_id=3, channel="whatsapp",
            phone_number=ops.TARGETS["tdf-sandbox"].phone, phone_number_id=ops.TARGETS["tdf-sandbox"].phone_id,
            waba_id=ops.TARGETS["tdf-sandbox"].waba_id, status="connected",
            webhook_url=ops.TARGETS["tdf-sandbox"].callback, status_callback_url=ops.TARGETS["tdf-sandbox"].callback))
        db.session.add(WhatsappNumero(id=2, numero_whatsapp=ops.TARGETS["junin-production"].phone, user_id=2, is_active=True))
        db.session.commit()
        calls = []
        def graph(method, url, **kwargs):
            assert method == "GET"
            calls.append(url)
            assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
            if url.endswith("/debug_token"):
                payload = {"data": {"is_valid": True, "app_id": ops.APP_ID,
                    "scopes": ["whatsapp_business_messaging", "whatsapp_business_management"],
                    "expires_at": NOW + 7200, "data_access_expires_at": NOW + 7200}}
            elif url.endswith("/phone_numbers"):
                target = next(t for t in ops.TARGETS.values() if "/" + t.waba_id + "/" in url)
                assert kwargs["params"]["fields"] == "id,display_phone_number"
                payload = {"data": [{"id": target.phone_id, "display_phone_number": target.phone}], "paging": {}}
            elif url.endswith("/subscribed_apps"):
                payload = {"data": [{"whatsapp_business_api_data": {"id": ops.APP_ID}}], "paging": {}}
            else:
                pytest.fail("Unexpected provider path")
            return SimpleNamespace(status_code=200, content=json.dumps(payload).encode())
        lease = SimpleNamespace(revalidate=lambda _cfg: SimpleNamespace(allowed=True, enabled=True, epoch=6))
        yield SimpleNamespace(app=app, graph=graph, calls=calls, lease=lease)
        db.session.rollback()
        db.session.remove()
        db.drop_all()
        db.engine.dispose()


def request(target="junin-production", **changes):
    values = dict(target=target, actor_user_id=1, expected_revision=ops.TARGETS[target].expected_revision,
                  expires_at=NOW + 3600, database_identity_sha256="a" * 64, expected_writer_epoch=6)
    values.update(changes)
    return ops.OperationRequest(**values)


def prepare(env, target="junin-production", *, token=TOKEN, graph=None, **changes):
    return ops.prepare_operation(db.session, request(target, **changes), token, env.app.config,
                                 graph_get=graph or env.graph, clock=lambda: NOW)


def apply(env, plan, **changes):
    args = dict(approved_plan_sha256=plan.plan_sha256, writer_lease=env.lease, clock=lambda: NOW)
    args.update(changes)
    return ops.apply_operation(db.session, plan, TOKEN, env.app.config, **args)


def test_dry_run_only_reads_provider_and_does_not_create_rows(environment):
    plan = prepare(environment)
    assert plan.plan["action"] == "create" and plan.plan["next_revision"] == 1
    assert len(environment.calls) == 3
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1
    assert AuditEvent.query.count() == 0 and not db.session.new and not db.session.dirty
    payload = json.dumps(plan.public_payload())
    for secret in (TOKEN, OLD_TOKEN, APP_SECRET, KEY, "ciphertext", "nonce", "access_token"):
        assert secret not in payload


def test_junin_apply_creates_meta_rows_and_preserves_twilio_mapping(environment):
    legacy = db.session.get(ProviderConnection, 1)
    original = (legacy.provider, legacy.environment, legacy.status, deepcopy(legacy.config), legacy.credentials_ref)
    result = apply(environment, prepare(environment))
    db.session.commit()
    connection = db.session.get(ProviderConnection, result["connection_id"])
    sender = db.session.get(ProviderSender, result["sender_id"])
    assert connection.tenant_id == 22 and connection.provider == "meta" and connection.environment == "production"
    assert sender.phone_number == "+17432643718" and sender.phone_number_id == "660753250460901"
    assert sender.webhook_url == ops.TARGETS["junin-production"].callback
    credential = vault.open_token(connection=connection, tenant_id=22, now=NOW, app_config=environment.app.config)
    assert credential.access_token == TOKEN and credential.revision == 1
    db.session.refresh(legacy)
    assert (legacy.provider, legacy.environment, legacy.status, legacy.config, legacy.credentials_ref) == original
    mapping = db.session.get(WhatsappNumero, 2)
    assert mapping.user_id == 2 and mapping.is_active and mapping.numero_whatsapp == "+17432643718"
    assert db.session.get(TenantProfile, 22).whatsapp_sender_id is None
    audit = AuditEvent.query.one()
    assert audit.actor_user_id == 1 and audit.tenant_id == 22
    assert TOKEN not in json.dumps(audit.details) and "ciphertext" not in json.dumps(audit.details)


def test_expired_tdf_revision6_rotates_to7_preserving_other_fields(environment):
    connection = db.session.get(ProviderConnection, 3)
    sender_before = ops._columns(db.session.get(ProviderSender, 1))
    before = ops._columns(connection)
    result = apply(environment, prepare(environment, "tdf-sandbox"))
    db.session.commit()
    db.session.refresh(connection)
    assert result["connection_id"] == 3 and result["sender_id"] == 1 and result["revision"] == 7
    assert connection.config["retain_note"] == "unchanged"
    credential = vault.open_token(connection=connection, tenant_id=46, now=NOW, app_config=environment.app.config)
    assert credential.access_token == TOKEN and credential.revision == 7
    after = ops._columns(connection)
    for name in before.keys() - {"config", "updated_at"}:
        assert before[name] == after[name]
    assert ops._columns(db.session.get(ProviderSender, 1)) == sender_before
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1


@pytest.mark.parametrize("target", ["junin-production", "tdf-sandbox"])
def test_caller_rollback_removes_both_credential_change_and_audit(environment, target):
    apply(environment, prepare(environment, target))
    db.session.rollback()
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1
    assert AuditEvent.query.count() == 0
    assert db.session.get(ProviderConnection, 3).config[PRIVATE_CONFIG_KEY]["revision"] == 6


def test_stale_revision_cannot_overwrite_concurrent_change(environment):
    plan = prepare(environment, "tdf-sandbox")
    connection = db.session.get(ProviderConnection, 3)
    changed = deepcopy(connection.config)
    changed[PRIVATE_CONFIG_KEY]["revision"] = 7
    connection.config = changed
    db.session.commit()
    with pytest.raises(ops.MetaCredentialOperationError, match="revision_conflict"):
        apply(environment, plan)
    assert AuditEvent.query.count() == 0
    assert db.session.get(ProviderConnection, 3).config[PRIVATE_CONFIG_KEY]["revision"] == 7


def test_unrelated_config_change_requires_new_reviewed_plan(environment):
    plan = prepare(environment, "tdf-sandbox")
    connection = db.session.get(ProviderConnection, 3)
    connection.config = {**connection.config, "operator_note": "new"}
    db.session.commit()
    with pytest.raises(ops.MetaCredentialOperationError, match="state_changed"):
        apply(environment, plan)
    assert AuditEvent.query.count() == 0


@pytest.mark.parametrize("case", ["wrong_plan", "wrong_token", "stale_proof", "fence", "lease", "epoch"])
def test_apply_guards_precede_writes(environment, case):
    plan = prepare(environment)
    args = {}
    if case == "wrong_plan": args["approved_plan_sha256"] = "b" * 64
    if case == "stale_proof": args["clock"] = lambda: NOW + 61
    if case == "fence": environment.app.config["CUTOVER_WRITER_FENCE_ENABLED"] = True
    if case in {"lease", "epoch"}:
        args["writer_lease"] = SimpleNamespace(revalidate=lambda cfg: SimpleNamespace(allowed=case != "lease", enabled=True, epoch=7 if case == "epoch" else 6))
    with pytest.raises(ops.MetaCredentialOperationError):
        if case == "wrong_token":
            ops.apply_operation(db.session, plan, OLD_TOKEN, environment.app.config,
                approved_plan_sha256=plan.plan_sha256, writer_lease=environment.lease, clock=lambda: NOW)
        else:
            apply(environment, plan, **args)
    assert ProviderConnection.query.count() == 2 and ProviderSender.query.count() == 1 and AuditEvent.query.count() == 0


def test_token_change_requires_different_plan_approval(environment):
    assert prepare(environment).plan_sha256 != prepare(environment, token=OLD_TOKEN).plan_sha256


@pytest.mark.parametrize("case", ["app", "scope", "expired", "phone", "subscription", "pagination"])
def test_provider_mismatch_prevents_any_persistence(environment, case):
    def graph(method, url, **kwargs):
        response = environment.graph(method, url, **kwargs)
        payload = json.loads(response.content)
        if url.endswith("/debug_token"):
            if case == "app": payload["data"]["app_id"] = "999999"
            if case == "scope": payload["data"]["scopes"] = []
            if case == "expired": payload["data"]["expires_at"] = NOW - 1
        if url.endswith("/phone_numbers"):
            if case == "phone": payload["data"][0]["display_phone_number"] = "+15550001111"
            if case == "pagination": payload["paging"] = {"next": "https://example.invalid/unread"}
        if url.endswith("/subscribed_apps") and case == "subscription": payload["data"] = []
        return SimpleNamespace(status_code=200, content=json.dumps(payload).encode())
    with pytest.raises((ops.MetaCredentialOperationError, ops.MetaRuntimeError)):
        prepare(environment, graph=graph)
    assert ProviderConnection.query.count() == 2 and AuditEvent.query.count() == 0


def test_declared_local_expiry_cannot_exceed_verified_provider_expiry(environment):
    with pytest.raises(ops.MetaCredentialOperationError, match="local_expiry_exceeds_provider"):
        prepare(environment, expires_at=NOW + 7201)


def test_duplicate_or_foreign_phone_stops_before_provider_get(environment):
    db.session.add(ProviderSender(tenant_id=46, provider_connection_id=3, channel="whatsapp",
        phone_number_id=ops.TARGETS["junin-production"].phone_id, status="connected"))
    db.session.commit()
    with pytest.raises(ops.MetaCredentialOperationError, match="new_binding_not_empty"):
        prepare(environment)
    assert environment.calls == []


@pytest.mark.parametrize("case", ["actor", "tenant", "revision", "shared_connection", "managed"])
def test_tdf_scope_and_actor_checks_precede_provider(environment, case):
    if case == "actor": db.session.get(User, 1).rol = "empleado"
    if case == "tenant": db.session.get(TenantProfile, 46).slug = "foreign"
    if case == "revision":
        connection = db.session.get(ProviderConnection, 3)
        cfg = deepcopy(connection.config); cfg[PRIVATE_CONFIG_KEY]["revision"] = 8; connection.config = cfg
    if case == "shared_connection":
        db.session.add(ProviderSender(tenant_id=46, provider_connection_id=3, channel="whatsapp", phone_number_id="9999999"))
    if case == "managed":
        connection = db.session.get(ProviderConnection, 3)
        connection.config = {**connection.config, ops.MANAGED_CONNECTION_MARKER: {"enabled": True}}
    db.session.commit()
    with pytest.raises(ops.MetaCredentialOperationError):
        prepare(environment, "tdf-sandbox")
    assert environment.calls == [] and AuditEvent.query.count() == 0


@pytest.mark.parametrize("value", [TOKEN + "\nextra", TOKEN + "\x00", "x" * 4097, "", "short", " leading spaces"])
def test_stdin_rejects_multiple_tokens_or_unbounded_input(value):
    with pytest.raises(cli.InputError):
        cli.read_token(from_stdin=True, stream=io.StringIO(value))


def test_stdin_accepts_one_token_and_never_echoes(capsys):
    assert cli.read_token(from_stdin=True, stream=io.StringIO(TOKEN + "\n")) == TOKEN
    assert capsys.readouterr() == ("", "")


def test_no_secret_argument_exists_and_argparse_does_not_echo_unknown_token(capsys):
    previous = logging.root.manager.disable
    try:
        assert cli.main(["--token", TOKEN]) == 2
    finally:
        logging.disable(previous)
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert "meta_operation_arguments_invalid" in output.out


def test_hidden_input_does_not_fall_back_to_echoed_stdin():
    with pytest.raises(cli.InputError, match="hidden_terminal_required"):
        cli.read_token(stream=io.StringIO(TOKEN), prompt=lambda _: pytest.fail("No fallback prompt"))


@pytest.mark.parametrize("query", ["host=other.example.invalid", "hostaddr=127.0.0.1", "port=6543",
    "dbname=other", "service=other", "servicefile=/tmp/pg", "options=-csearch_path=other",
    "password=hidden", "sslmode=require&sslmode=disable"])
def test_database_identity_cannot_be_overridden_in_query(query):
    with pytest.raises(cli.InputError, match="postgresql_tls_required"):
        cli._database("postgresql://operator:synthetic@db.example.invalid/app?sslmode=verify-full&" + query)


def test_database_identity_excludes_password_but_keeps_exact_database():
    first, digest = cli._database("postgresql://operator:first-synthetic@db.example.invalid/app?sslmode=verify-full")
    _, same = cli._database("postgresql://operator:second-synthetic@db.example.invalid/app?sslmode=verify-full")
    _, other = cli._database("postgresql://operator:first-synthetic@db.example.invalid/other?sslmode=verify-full")
    assert digest == same and digest != other and first.drivername == "postgresql+psycopg" and first.port == 5432


def test_cli_stdin_requires_pipe_and_rejects_regular_file():
    with tempfile.TemporaryFile(mode="w+") as file:
        assert cli.protected_stdin(file) is False
    reader, writer = os.pipe()
    try:
        with os.fdopen(reader) as pipe:
            assert cli.protected_stdin(pipe) is True
    finally:
        os.close(writer)


def test_cli_apply_failure_never_claims_rollback_or_discloses_exception(monkeypatch, capsys):
    monkeypatch.setattr(cli, "parser", lambda: SimpleNamespace(parse_args=lambda argv: SimpleNamespace(apply=True, token_stdin=False)))
    monkeypatch.setattr(cli, "read_token", lambda **kwargs: TOKEN)
    def failed(*args):
        raise RuntimeError("commit transport failed with private value " + TOKEN)
    monkeypatch.setattr(cli, "_run", failed)
    previous = logging.root.manager.disable
    try:
        assert cli.main([]) == 2
    finally:
        logging.disable(previous)
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    result = json.loads(captured.out)
    assert result["database_outcome"] == "unverified" and result["reason_code"] == "meta_operation_failed"
    assert "writes_performed" not in result
