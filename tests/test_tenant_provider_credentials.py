"""Offline credential-store contracts; synthetic keys and an isolated SQLite DB.

The provider is never called.  Default SQLite validates persistence/CAS/rollback;
opt-in loopback PostgreSQL additionally tests real locks. Neither is production
key readiness or WhatsApp delivery acceptance.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import secrets
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("FLASK_SKIP_GLOBAL_APP", "1")

import pytest
import requests
from flask import Flask, jsonify
from sqlalchemy import event, null, select, text, update
from sqlalchemy.orm import Session

from models import AuditEvent, MessageTemplateRegistry, ProviderConnection, ProviderSender, TenantProfile, User, db
from services import provider_platform
from services import tenant_provider_credentials as vault
from services import twilio_tech_provider as runtime
from services.provider_connection_cutover_contract import MANAGED_CONNECTION_MARKER


# Synthetic material exists only in this test process, never in source or logs.
ACCOUNT = "AC" + secrets.token_hex(16)
OTHER_ACCOUNT = "AC" + secrets.token_hex(16)
TOKEN = secrets.token_hex(16)
ROTATED_TOKEN = secrets.token_hex(16)
KEY_A = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
KEY_B = base64.b64encode(secrets.token_bytes(32)).decode("ascii")
POSTGRES_OPT_IN = os.environ.get("CHATBOC_VAULT_TEST_POSTGRES") == "1"
POSTGRES_TEST_URL = "postgresql+psycopg://postgres@127.0.0.1:5432/vaultcredregression"


def _key_config(*, active="test-a", keys=None):
    return {
        "TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID": active,
        "TENANT_PROVIDER_CREDENTIAL_KEYRING": json.dumps(
            keys if keys is not None else {"test-a": KEY_A, "test-b": KEY_B}
        ),
    }


def _connection(**overrides):
    values = dict(
        id=17, tenant_id=23, provider="twilio", channel="whatsapp",
        environment="production", status="needs_setup", external_account_id=ACCOUNT,
        credentials_ref=vault.VAULT_REF, config={}, display_name="Test organization",
        external_business_id=None, external_app_id=None, configuration_id=None,
        partner_solution_id=None, capabilities={}, health={}, updated_at=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _install_envelope(connection, *, revision=1, token=TOKEN, config=None):
    connection.config = {
        **(config or {}),
        vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=connection, tenant_id=connection.tenant_id,
            account_sid=connection.external_account_id, auth_token=token,
            revision=revision, app_config=_key_config(),
        ),
    }
    connection.credentials_ref = vault.VAULT_REF
    return connection


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def blocked(*_args, **_kwargs):
        raise AssertionError("This credential contract must not perform network I/O")
    def guarded_dns(host, port, *_args, **_kwargs):
        if POSTGRES_OPT_IN and host == "127.0.0.1" and port == 5432:
            return original_getaddrinfo(host, port, *_args, **_kwargs)
        return blocked()
    def guarded_connect(sock, address):
        if POSTGRES_OPT_IN and address == ("127.0.0.1", 5432):
            return original_connect(sock, address)
        return blocked()
    def guarded_connect_ex(sock, address):
        if POSTGRES_OPT_IN and address == ("127.0.0.1", 5432):
            return original_connect_ex(sock, address)
        return blocked()
    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def credential_db():
    app = Flask("isolated-provider-credential-contract")
    schema = "vaultcred_contract_" + secrets.token_hex(8) if POSTGRES_OPT_IN else None
    app.config.update(
        TESTING=True, SQLALCHEMY_DATABASE_URI=POSTGRES_TEST_URL if POSTGRES_OPT_IN else "sqlite:///:memory:",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    if schema:
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "connect_args": {"options": f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=10000"},
        }
    app.config.update(_key_config())
    db.init_app(app)
    with app.app_context():
        schema_created = False
        try:
            if schema:
                assert db.engine.url.host == "127.0.0.1" and db.engine.url.port == 5432
                assert db.engine.url.database == "vaultcredregression" and db.engine.url.username == "postgres"
                assert db.engine.url.password is None
                assert re.fullmatch(r"vaultcred_contract_[0-9a-f]{16}", schema)
                with db.engine.begin() as setup:
                    setup.execute(text(f'CREATE SCHEMA "{schema}"'))
                    schema_created = True
                    assert setup.execute(text("SELECT current_schema()")).scalar_one() == schema
            else:
                assert db.engine.dialect.name == "sqlite" and db.engine.url.database == ":memory:"
            db.create_all()
            owner = User(name="Synthetic owner", email="vault-owner@example.test", rol="admin",
                         password_hash="synthetic-not-a-login-hash")
            db.session.add(owner); db.session.flush()
            tenant = TenantProfile(
                slug="vault-contract", nombre="Synthetic organization", tipo="municipio",
                municipio_id=owner.id, is_active=True,
                configuracion={"twilio_tech_provider": {"twilio_account_sid": ACCOUNT}},
            )
            db.session.add(tenant); db.session.flush()
            connection = ProviderConnection(
                tenant_id=tenant.id, provider="twilio", channel="whatsapp",
                environment="production", status="needs_setup", external_account_id=ACCOUNT,
                credentials_ref="env:synthetic_legacy", config={"operator_note": "retain"},
            )
            db.session.add(connection); db.session.commit()
            yield SimpleNamespace(app=app, owner=owner, tenant=tenant, connection=connection,
                                  actor_id=owner.id, tenant_id=tenant.id, connection_id=connection.id,
                                  engine=db.engine)
        finally:
            db.session.rollback(); db.session.remove()
            try:
                if schema_created:
                    assert re.fullmatch(r"vaultcred_contract_[0-9a-f]{16}", schema)
                    with db.engine.begin() as cleanup:
                        cleanup.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            finally:
                db.engine.dispose()


def _store(rows, *, revision=0, token=TOKEN, **overrides):
    args = dict(
        tenant_id=rows.tenant_id, connection_id=rows.connection_id,
        account_sid=ACCOUNT, auth_token=token, expected_revision=revision,
        actor_user_id=rows.actor_id, app_config=rows.app.config,
    )
    args.update(overrides)
    return vault.store_tenant_twilio_token(**args)


def _database_projection(connection_id):
    return db.session.execute(
        select(ProviderConnection.config, ProviderConnection.credentials_ref,
               ProviderConnection.external_account_id, ProviderConnection.status)
        .where(ProviderConnection.id == connection_id)
        .execution_options(autoflush=False)
    ).one()


def _winner_rotation(rows):
    """Change the DB projection via Core SQL while one ORM identity keeps rev1.

    This is a single-transaction autoflush control, not real two-session or
    PostgreSQL contention evidence.  The opt-in PG test covers that separately.
    """
    old = copy.deepcopy(rows.connection.config)
    winner = {
        **old,
        vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=rows.connection, tenant_id=rows.tenant.id,
            account_sid=ACCOUNT, auth_token=ROTATED_TOKEN, revision=2,
            app_config=rows.app.config,
        ),
    }
    db.session.execute(
        update(ProviderConnection).where(ProviderConnection.id == rows.connection.id)
        .values(config=winner, credentials_ref=vault.VAULT_REF)
        .execution_options(synchronize_session=False, autoflush=False)
    )
    assert rows.connection.config == old
    return old, winner


def test_roundtrip_is_bound_encrypted_and_omits_token_from_repr():
    connection = _install_envelope(_connection())
    opened = vault.open_token(connection=connection, tenant_id=23, app_config=_key_config())
    assert opened.account_sid == ACCOUNT and opened.auth_token == TOKEN and opened.revision == 1
    assert TOKEN not in repr(opened)
    assert TOKEN not in json.dumps(connection.config)
    rotated = _install_envelope(_connection(), revision=2, token=ROTATED_TOKEN)
    assert connection.config[vault.PRIVATE_CONFIG_KEY] != rotated.config[vault.PRIVATE_CONFIG_KEY]


@pytest.mark.parametrize("field,value", [
    ("id", 18), ("tenant_id", 24), ("provider", "meta"),
    ("channel", "sms"), ("environment", "sandbox"),
    ("external_account_id", OTHER_ACCOUNT),
])
def test_ciphertext_cannot_move_to_another_binding(field, value):
    connection = _install_envelope(_connection())
    setattr(connection, field, value)
    with pytest.raises(vault.ProviderCredentialError) as caught:
        vault.open_token(connection=connection, tenant_id=connection.tenant_id, app_config=_key_config())
    assert TOKEN not in str(caught.value) and KEY_A not in str(caught.value)


@pytest.mark.parametrize("mutation", ["revision", "key_id", "nonce", "ciphertext", "contract", "reference"])
def test_tampered_envelope_or_reference_fails_without_plaintext_error(mutation):
    connection = _install_envelope(_connection())
    envelope = connection.config[vault.PRIVATE_CONFIG_KEY]
    if mutation == "revision": envelope["revision"] = 2
    elif mutation == "key_id": envelope["key_id"] = "test-b"
    elif mutation == "nonce": envelope["nonce"] = base64.b64encode(bytes([4]) * 12).decode("ascii")
    elif mutation == "ciphertext": envelope["ciphertext"] = base64.b64encode(bytes([7]) * 48).decode("ascii")
    elif mutation == "contract": envelope["contract"] = "foreign.contract"
    else: connection.credentials_ref = "env:legacy"
    with pytest.raises(vault.ProviderCredentialError) as caught:
        vault.open_token(connection=connection, tenant_id=23, app_config=_key_config())
    assert TOKEN not in str(caught.value)
    assert envelope.get("ciphertext", "") not in str(caught.value)


@pytest.mark.parametrize("raw,active", [
    (None, "test-a"), ("not-json", "test-a"), ("[]", "test-a"), ("{}", "test-a"),
    (json.dumps({"test-a": "not-base64"}), "test-a"),
    (json.dumps({"test-a": base64.b64encode(bytes(31)).decode("ascii")}), "test-a"),
    (json.dumps({"test-a": KEY_A}), "missing"),
    (json.dumps({"unsafe/key": KEY_A}), "unsafe/key"),
    ('{"test-a":"' + KEY_A + '","test-a":"' + KEY_B + '"}', "test-a"),
])
def test_invalid_or_ambiguous_keyring_reports_unavailable_without_key_material(raw, active):
    status = vault.credential_storage_status({
        "TENANT_PROVIDER_CREDENTIAL_KEYRING": raw,
        "TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID": active,
    })
    assert status == {"ready": False, "status": "unavailable", "reason_code": "twilio_tenant_credential_store_unavailable"}
    assert KEY_A not in json.dumps(status) and KEY_B not in json.dumps(status)


def test_rotation_keeps_old_key_readable_and_requires_it_for_old_envelope():
    connection = _install_envelope(_connection())
    rotation_config = _key_config(active="test-b")
    assert vault.open_token(connection=connection, tenant_id=23, app_config=rotation_config).auth_token == TOKEN
    with pytest.raises(vault.ProviderCredentialError):
        vault.open_token(connection=connection, tenant_id=23,
                         app_config=_key_config(active="test-b", keys={"test-b": KEY_B}))


@pytest.mark.parametrize("private_name", ["authToken", "api-secret", "authorization", "provider_auth_token"])
def test_serialized_response_redacts_normalized_secret_aliases_and_nested_envelopes(private_name):
    private_value = "synthetic-private-value"
    original = {
        "webhook_url": "https://public.example.test/webhook",
        "live_enabled": False,
        private_name: private_value,
        vault.PRIVATE_CONFIG_KEY: {"ciphertext": "synthetic-ciphertext"},
        "public_nested": [{"label": "Visible", private_name: private_value,
                           vault.PRIVATE_CONFIG_KEY: {"key_id": "private-kid"}}],
        "tuple_nested": ({"label": "Visible", private_name: private_value},),
    }
    connection = _connection(config=copy.deepcopy(original))
    response = provider_platform._serialize_connection(connection)
    assert response["config"]["webhook_url"] == original["webhook_url"]
    assert response["config"]["live_enabled"] is False
    assert "credentials_ref" not in response
    serialized = json.dumps(response)
    assert private_name not in serialized and private_value not in serialized
    assert vault.PRIVATE_CONFIG_KEY not in serialized and "synthetic-ciphertext" not in serialized
    assert connection.config == original


def test_managed_vault_is_rejected_by_direct_open_and_runtime_without_sync(monkeypatch):
    connection = _install_envelope(_connection())
    connection.config[MANAGED_CONNECTION_MARKER] = {"enabled": True}
    with pytest.raises(vault.ProviderCredentialError):
        vault.open_token(connection=connection, tenant_id=23, app_config=_key_config())
    monkeypatch.setattr(runtime, "_read_config_or_env", lambda *_args: pytest.fail("No environment fallback"))
    credentials = runtime.resolve_twilio_runtime_credentials(
        tenant=SimpleNamespace(id=23, configuracion={}), provider_connection=connection,
        app_config=_key_config(),
    )
    assert not credentials.ready and credentials.auth_token is None


@pytest.mark.parametrize("failure", ["missing_envelope", "invalid_reference", "foreign_tenant", "tampered_envelope", "wrong_account"])
def test_runtime_vault_failures_never_fall_back_to_parent_or_environment(monkeypatch, failure):
    connection = _install_envelope(_connection())
    tenant = SimpleNamespace(id=23, configuracion={})
    if failure == "missing_envelope": connection.config = {}
    elif failure == "invalid_reference": connection.credentials_ref = "env:legacy"
    elif failure == "foreign_tenant": tenant.id = 24
    elif failure == "tampered_envelope": connection.config[vault.PRIVATE_CONFIG_KEY]["revision"] = 2
    else: tenant.configuracion = {runtime.STATE_KEY: {"twilio_account_sid": OTHER_ACCOUNT}}
    monkeypatch.setattr(runtime, "_read_config_or_env", lambda *_args: pytest.fail("No environment fallback"))
    credentials = runtime.resolve_twilio_runtime_credentials(
        tenant=tenant, provider_connection=connection, app_config=_key_config(),
    )
    assert not credentials.ready and credentials.auth_token is None


def test_runtime_uses_exact_tenant_vault_without_env_lookup(monkeypatch):
    connection = _install_envelope(_connection())
    monkeypatch.setattr(runtime, "_read_config_or_env", lambda *_args: pytest.fail("No environment lookup"))
    credentials = runtime.resolve_twilio_runtime_credentials(
        tenant=SimpleNamespace(id=23, configuracion={}), provider_connection=connection,
        app_config=_key_config(),
    )
    assert credentials.ready and credentials.scope == "tenant_vault"
    assert credentials.auth_token == TOKEN and credentials.account_sid == ACCOUNT
    assert TOKEN not in repr(credentials)


def test_store_commits_secret_free_audit_with_rotation_and_no_sender_activation(credential_db):
    rows = credential_db
    original_tenant = copy.deepcopy(rows.tenant.configuracion)
    original_actor = (rows.owner.rol, rows.owner.tenant_id)
    assert _store(rows) == 1
    db.session.commit()
    assert _store(rows, revision=1, token=ROTATED_TOKEN) == 2
    db.session.commit(); db.session.refresh(rows.connection)
    opened = vault.open_token(connection=rows.connection, tenant_id=rows.tenant.id, app_config=rows.app.config)
    assert opened.auth_token == ROTATED_TOKEN and opened.revision == 2
    assert rows.connection.credentials_ref == vault.VAULT_REF
    assert rows.connection.status == "needs_setup"
    assert rows.connection.config["operator_note"] == "retain"
    assert rows.tenant.configuracion == original_tenant
    assert (rows.owner.rol, rows.owner.tenant_id) == original_actor
    assert ProviderSender.query.count() == 0
    audits = AuditEvent.query.order_by(AuditEvent.id).all()
    assert len(audits) == 2
    assert [item.details["revision"] for item in audits] == [1, 2]
    for audit in audits:
        assert audit.tenant_id == rows.tenant.id and audit.actor_user_id == rows.owner.id
        assert audit.event_type == "provider_credential.stored" and audit.resource_id == str(rows.connection.id)
        assert set(audit.details) == {"contract", "revision", "key_id", "provider"}
        serialized = json.dumps(audit.details)
        assert TOKEN not in serialized and ROTATED_TOKEN not in serialized and KEY_A not in serialized
        assert "ciphertext" not in serialized and "nonce" not in serialized


def test_caller_rollback_removes_token_and_audit_together(credential_db):
    rows = credential_db
    before = copy.deepcopy(tuple(_database_projection(rows.connection.id)))
    assert _store(rows) == 1
    assert AuditEvent.query.count() == 1
    db.session.rollback()
    assert tuple(_database_projection(rows.connection.id)) == before
    assert AuditEvent.query.count() == 0


@pytest.mark.parametrize("case", ["tenant", "connection", "account", "managed", "stale_revision"])
def test_store_rejects_wrong_binding_managed_or_stale_cas_without_partial_audit(credential_db, case):
    rows = credential_db
    if case == "managed":
        rows.connection.config = {MANAGED_CONNECTION_MARKER: {"enabled": True}}
        db.session.commit()
    if case == "stale_revision":
        _store(rows); db.session.commit()
    before = copy.deepcopy(tuple(_database_projection(rows.connection.id)))
    audit_count = AuditEvent.query.count()
    overrides = {"tenant_id": rows.tenant.id + 100} if case == "tenant" else {}
    if case == "connection": overrides["connection_id"] = rows.connection.id + 100
    if case == "account": overrides["account_sid"] = OTHER_ACCOUNT
    with pytest.raises(vault.ProviderCredentialError):
        _store(rows, **overrides)
    assert tuple(_database_projection(rows.connection.id)) == before
    assert AuditEvent.query.count() == audit_count


@pytest.mark.parametrize("dirty", [False, True], ids=["cached", "dirty-cached"])
def test_store_refreshes_before_cas_and_never_autoflushes_over_a_winning_rotation(credential_db, dirty):
    rows = credential_db
    _store(rows); db.session.commit()
    old, winner = _winner_rotation(rows)
    if dirty:
        rows.connection.config = {**old, "stale_writer_note": "must-not-overwrite"}
        rows.connection.credentials_ref = "env:stale_writer"
    with pytest.raises(vault.ProviderCredentialError, match="provider_credential_revision_conflict"):
        _store(rows, revision=1)
    actual = _database_projection(rows.connection.id)
    assert actual.config == winner and actual.credentials_ref == vault.VAULT_REF
    assert AuditEvent.query.count() == 1


def test_unprotected_orm_autoflush_control_demonstrates_the_stale_write_hazard(credential_db):
    rows = credential_db
    _store(rows); db.session.commit()
    old, winner = _winner_rotation(rows)
    rows.connection.config = {**old, "unsafe_control": True}
    db.session.execute(select(ProviderConnection).where(ProviderConnection.id == rows.connection.id)
                       .execution_options(populate_existing=True)).scalar_one()
    actual = _database_projection(rows.connection.id)
    assert actual.config != winner
    assert actual.config[vault.PRIVATE_CONFIG_KEY]["revision"] == 1
    db.session.rollback()


def _sync_config(rows):
    return {**rows.app.config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED": True,
            "PUBLIC_API_BASE_URL": "https://candidate.example.test"}


def test_sync_and_full_status_response_preserve_private_store_but_never_serialize_it(credential_db):
    rows = credential_db
    _store(rows); db.session.commit()
    envelope = copy.deepcopy(rows.connection.config[vault.PRIVATE_CONFIG_KEY])
    connection, sender = provider_platform.sync_twilio_provider_records(
        rows.tenant, {"twilio_account_sid": ACCOUNT, "status": "subaccount_created"},
        app_config=_sync_config(rows),
    )
    db.session.commit(); db.session.refresh(connection)
    assert connection.config[vault.PRIVATE_CONFIG_KEY] == envelope
    assert connection.credentials_ref == vault.VAULT_REF and connection.external_account_id == ACCOUNT
    assert connection.config["webhook_url"] == "https://candidate.example.test/webhook/whatsapp"
    assert sender is None and ProviderSender.query.count() == 0
    assert AuditEvent.query.count() == 1
    with rows.app.test_request_context():
        response = jsonify(provider_platform.build_whatsapp_provider_status(rows.tenant, _sync_config(rows)))
        body = response.get_data(as_text=True)
    assert vault.PRIVATE_CONFIG_KEY not in body and vault.VAULT_REF not in body
    assert envelope["ciphertext"] not in body and envelope["nonce"] not in body
    assert TOKEN not in body


@pytest.mark.parametrize("dirty", [False, True], ids=["cached", "dirty-cached"])
def test_sync_refreshes_before_private_merge_and_preserves_winning_rotation(credential_db, dirty):
    rows = credential_db
    _store(rows); db.session.commit()
    old, winner = _winner_rotation(rows)
    if dirty:
        rows.connection.config = {**old, "stale_writer_note": "must-not-overwrite"}
        rows.connection.credentials_ref = "env:stale_writer"
    provider_platform.sync_twilio_provider_records(
        rows.tenant, {"twilio_account_sid": ACCOUNT}, app_config=_sync_config(rows),
    )
    db.session.commit()
    actual = _database_projection(rows.connection.id)
    assert actual.config[vault.PRIVATE_CONFIG_KEY] == winner[vault.PRIVATE_CONFIG_KEY]
    assert "stale_writer_note" not in actual.config and actual.credentials_ref == vault.VAULT_REF
    assert AuditEvent.query.count() == 1


@pytest.mark.parametrize("case", ["reference", "missing_envelope", "account", "managed"])
def test_sync_invalid_private_binding_fails_before_connection_writes(credential_db, monkeypatch, case):
    rows = credential_db
    _store(rows); db.session.commit()
    state = {"twilio_account_sid": ACCOUNT, "status": "must-not-apply"}
    if case == "reference": rows.connection.credentials_ref = "env:legacy"
    elif case == "missing_envelope": rows.connection.config = {}
    elif case == "account": state["twilio_account_sid"] = OTHER_ACCOUNT
    else: rows.connection.config = {**rows.connection.config, MANAGED_CONNECTION_MARKER: {"enabled": True}}
    db.session.commit()
    before = copy.deepcopy(tuple(_database_projection(rows.connection.id)))
    if case == "managed":
        # Existing managed SERIALIZABLE requirements are covered separately;
        # this case isolates the shared private-binding guard before writes.
        monkeypatch.setattr(provider_platform, "_get_or_create_connection", lambda *_args: rows.connection)
    with pytest.raises(provider_platform.ManagedProviderConnectionStateError):
        provider_platform.sync_twilio_provider_records(rows.tenant, state, app_config=_sync_config(rows))
    assert tuple(_database_projection(rows.connection.id)) == before
    assert AuditEvent.query.count() == 1 and ProviderSender.query.count() == 0


@pytest.mark.parametrize("corrupt_config", [["not-a-config"], "not-a-config", 7])
def test_malformed_config_rejects_store_without_partial_token_or_audit(credential_db, corrupt_config):
    rows = credential_db
    rows.connection.config = corrupt_config
    db.session.commit()
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    with pytest.raises(vault.ProviderCredentialError):
        _store(rows)
    assert tuple(_database_projection(rows.connection_id)) == before
    assert AuditEvent.query.count() == 0


def test_audit_insert_failure_is_rolled_back_with_token_by_transaction_owner(credential_db):
    rows = credential_db
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    effect_session = db.session()
    def reject_new_audit(session, _flush_context, _instances):
        if any(isinstance(item, AuditEvent) for item in session.new):
            raise RuntimeError("synthetic_audit_insert_blocked")
    event.listen(effect_session, "before_flush", reject_new_audit)
    try:
        with pytest.raises(RuntimeError, match="synthetic_audit_insert_blocked"):
            _store(rows)
    finally:
        event.remove(effect_session, "before_flush", reject_new_audit)
    # The module must not commit or own this rollback. Its caller owns the TX.
    db.session.rollback()
    assert tuple(_database_projection(rows.connection_id)) == before
    assert AuditEvent.query.count() == 0


@pytest.mark.parametrize("representation", ["json-null", "sql-null"])
def test_empty_config_null_representations_support_initial_store_and_owned_rollback(credential_db, representation):
    rows = credential_db
    if representation == "json-null":
        rows.connection.config = None
    else:
        db.session.execute(
            update(ProviderConnection).where(ProviderConnection.id == rows.connection_id)
            .values(config=null()).execution_options(synchronize_session=False, autoflush=False)
        )
    db.session.commit()
    def is_sql_null():
        return db.session.execute(
            select(ProviderConnection.config.is_(None))
            .where(ProviderConnection.id == rows.connection_id).execution_options(autoflush=False)
        ).scalar_one()
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    assert before[0] is None
    assert is_sql_null() is (representation == "sql-null")
    assert _store(rows) == 1
    assert AuditEvent.query.count() == 1
    db.session.rollback()
    assert tuple(_database_projection(rows.connection_id)) == before
    assert is_sql_null() is (representation == "sql-null")
    assert AuditEvent.query.count() == 0
    assert _store(rows) == 1
    db.session.commit()
    assert vault.open_token(connection=db.session.get(ProviderConnection, rows.connection_id),
                            tenant_id=rows.tenant_id, app_config=rows.app.config).auth_token == TOKEN
    assert AuditEvent.query.count() == 1


def _call_onboarding(operation, rows):
    config = {**rows.app.config, "TWILIO_TECH_PROVIDER_LIVE_ENABLED": True}
    if operation == "poll":
        return runtime.poll_whatsapp_sender_status(rows.tenant, config)
    if operation == "register":
        return runtime.register_whatsapp_sender(rows.tenant, {}, config)
    return runtime.provision_twilio_voice_application(rows.tenant, {}, config)


@pytest.mark.parametrize("operation", ["register", "poll", "voice"])
@pytest.mark.parametrize("dirty", [False, True], ids=["fresh", "dirty-hidden-vault"])
def test_legacy_onboarding_blocks_owned_vault_before_any_env_or_provider_access(credential_db, monkeypatch, operation, dirty):
    rows = credential_db
    _store(rows); db.session.commit()
    # Load the tenant before making the other identity dirty, so this fixture
    # cannot trigger an unrelated caller-side lazy-load autoflush.
    assert rows.tenant.id == rows.tenant_id
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    if dirty:
        rows.connection.config = {"stale_public_only": True}
        rows.connection.credentials_ref = "env:stale_writer"
    def forbidden(*_args, **_kwargs):
        pytest.fail("Vault onboarding must stop before env credentials or provider I/O")
    for name in ("_read_config_or_env", "_resolve_subaccount_auth_token", "_twilio_get_json", "_twilio_post_form"):
        monkeypatch.setattr(runtime, name, forbidden)
    result = _call_onboarding(operation, rows)
    assert result["mode"] == "blocked" and result["ok"] is False
    assert result["reason_code"] == "twilio_vault_onboarding_integration_required"
    assert result["state_patch"] == {} and result["provider_calls_performed"] is False
    assert tuple(_database_projection(rows.connection_id)) == before
    # Discard the intentional local dirty snapshot without persisting it.
    db.session.rollback()


@pytest.mark.parametrize("operation", ["register", "poll", "voice"])
def test_foreign_tenant_vault_does_not_block_unrelated_legacy_onboarding(credential_db, monkeypatch, operation):
    rows = credential_db
    owner = User(name="Other synthetic owner", email="vault-other@example.test", rol="admin",
                 password_hash="synthetic-not-a-login-hash")
    db.session.add(owner); db.session.flush()
    foreign = TenantProfile(slug="foreign-vault", nombre="Other synthetic organization",
                            tipo="municipio", municipio_id=owner.id, is_active=True)
    db.session.add(foreign); db.session.flush()
    connection = ProviderConnection(tenant_id=foreign.id, provider="twilio", channel="whatsapp",
                                    environment="production", external_account_id=OTHER_ACCOUNT,
                                    status="needs_setup")
    db.session.add(connection); db.session.flush()
    _install_envelope(connection); db.session.commit()
    assert runtime._tenant_has_internal_provider_credentials(rows.tenant) is False
    monkeypatch.setattr(runtime, "_read_config_or_env", lambda *_args: "")
    monkeypatch.setattr(runtime, "_resolve_subaccount_auth_token", lambda **_kwargs: (None, []))
    result = _call_onboarding(operation, rows)
    assert result.get("reason_code") != "twilio_vault_onboarding_integration_required"
    assert vault.PRIVATE_CONFIG_KEY not in rows.connection.config
    assert ProviderSender.query.count() == 0


def _complete_whatsapp_state(rows, *, sender_status="ONLINE"):
    state = {
        "twilio_account_sid": ACCOUNT, "messaging_service_sid": "MG-synthetic-service",
        "sender_sid": "XE-synthetic-sender", "sender_id": "whatsapp:+15555550123",
        "requested_phone_number": "+15555550123", "sender_status": sender_status,
        "status": "sender_online", "waba_id": "123456789", "phone_number_id": "987654321",
        "voice_twiml_app_sid": "AP-synthetic-voice", "templates_ready": True,
        "twilio_subaccount_token_ref": "R15_SYNTHETIC_TENANT_TOKEN",
    }
    rows.tenant.configuracion = {"twilio_tech_provider": state}
    db.session.add(MessageTemplateRegistry(tenant_id=rows.tenant_id, provider="twilio",
                                          channel="whatsapp", name="r15-synthetic-menu", status="approved"))
    db.session.commit()
    return state


def _complete_whatsapp_config(rows, *, store_ready=True, live=True):
    config = {
        **rows.app.config, "TWILIO_ACCOUNT_SID": "synthetic-parent",
        "TWILIO_AUTH_TOKEN": "synthetic-parent-token", "TWILIO_META_APP_ID": "synthetic-meta",
        "TWILIO_META_EMBEDDED_SIGNUP_CONFIG_ID": "synthetic-signup",
        "TWILIO_TECH_PROVIDER_LIVE_ENABLED": live,
        "R15_SYNTHETIC_TENANT_TOKEN": "synthetic-tenant-token",
        "PUBLIC_API_BASE_URL": "https://candidate.example.test",
    }
    if not store_ready:
        config.pop("TENANT_PROVIDER_CREDENTIAL_ACTIVE_KEY_ID", None)
        config.pop("TENANT_PROVIDER_CREDENTIAL_KEYRING", None)
    return config


@pytest.mark.parametrize("store_ready", [False, True], ids=["store-unavailable", "store-configured"])
def test_complete_owned_vault_contract_matches_blocked_executors_without_pilot_claim(credential_db, monkeypatch, store_ready):
    rows = credential_db
    _store(rows); db.session.commit()
    state = _complete_whatsapp_state(rows)
    config = _complete_whatsapp_config(rows, store_ready=store_ready)
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    envelope = copy.deepcopy(before[0][vault.PRIVATE_CONFIG_KEY])
    def forbidden(*_args, **_kwargs):
        pytest.fail("Readiness must not read env fallback or perform provider I/O for owned vault")
    for name in ("_read_config_or_env", "_resolve_subaccount_auth_token", "_twilio_get_json", "_twilio_post_form", "_twilio_post_json"):
        monkeypatch.setattr(runtime, name, forbidden)
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, config)
    # This behavioral assertion is the causal R14 failure, before new-field checks.
    assert tech["setup_health"]["status"] == "action_required"
    assert tech["status"] == "needs_secure_activation"
    assert tech["setup_health"]["recommended_next_action"] == "wait_for_platform_activation"
    assert tech["frontend_contract"]["primary_action"] == "wait_for_platform_activation"
    assert tech["automation"]["credential_storage"]["ready"] is store_ready
    assert tech["automation"]["credential_storage"]["provisioning_ready"] is False
    assert tech["state"]["twilio_account_sid"] == state["twilio_account_sid"]
    assert tech["state"]["sender_sid"] == state["sender_sid"]
    assert all(item["done"] for item in tech["operator_checklist"])
    tests = {item["id"]: item for item in tech["smoke_playbook"]["tests"]}
    assert tests["production_channel"]["can_execute"] is False
    assert tests["live_whatsapp_message"]["can_execute"] is False
    assert tests["live_whatsapp_message"]["reason_code"] == "execution_not_implemented"
    assert all(tests[name]["can_execute"] is True for name in ("provider_status", "whatsapp_experience", "sandbox_message"))
    for name, executor in (
        ("register_sender", lambda: runtime.register_whatsapp_sender(rows.tenant, {}, config)),
        ("poll_sender_status", lambda: runtime.poll_whatsapp_sender_status(rows.tenant, config)),
        ("prepare_voice", lambda: runtime.provision_twilio_voice_application(rows.tenant, {}, config)),
    ):
        declared = tech["operation_availability"]["operations"][name]
        executed = executor()
        assert declared["can_execute"] is False
        assert declared["reason_code"] == executed["reason_code"] == "twilio_vault_onboarding_integration_required"
        assert executed["state_patch"] == {} and executed["provider_calls_performed"] is False
    assert tuple(_database_projection(rows.connection_id)) == before
    provider = provider_platform.build_whatsapp_provider_status(rows.tenant, config)
    assert provider["next_action"] == provider["frontend_contract"]["primary_action"] == "wait_for_platform_activation"
    assert provider["status"] == "needs_secure_activation"
    assert provider["operation_availability"] == tech["operation_availability"]
    assert provider["operational_readiness"]["delivery_accepted"] is False
    body = json.dumps({"provider": provider, "tech": tech})
    assert all(private not in body for private in (TOKEN, vault.PRIVATE_CONFIG_KEY, vault.VAULT_REF, envelope["nonce"], envelope["ciphertext"]))
    assert _database_projection(rows.connection_id).config[vault.PRIVATE_CONFIG_KEY] == envelope
    assert AuditEvent.query.count() == 1


@pytest.mark.parametrize("expired_tenant", [False, True], ids=["loaded-tenant", "expired-tenant"])
def test_availability_reads_committed_owned_vault_without_flushing_hidden_dirty_identity(credential_db, expired_tenant):
    rows = credential_db
    _store(rows); db.session.commit()
    _complete_whatsapp_state(rows)
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    assert rows.tenant.id == rows.tenant_id
    if expired_tenant:
        db.session.expire(rows.tenant)
    rows.connection.config = {"stale_public_only": True}
    rows.connection.credentials_ref = "env:stale"
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, _complete_whatsapp_config(rows))
    assert tech["setup_health"]["status"] == "action_required"
    assert tech["operation_availability"]["operations"]["poll_sender_status"]["reason_code"] == "twilio_vault_onboarding_integration_required"
    assert tuple(_database_projection(rows.connection_id)) == before
    db.session.rollback()


@pytest.mark.parametrize("representation", ["json-null", "sql-null", "legacy-env", "managed-env", "foreign-vault"])
def test_complete_legacy_and_managed_availability_preserves_supported_operations(credential_db, monkeypatch, representation):
    rows = credential_db
    state = _complete_whatsapp_state(rows)
    if representation == "json-null":
        rows.connection.config = None
    elif representation == "sql-null":
        db.session.execute(update(ProviderConnection).where(ProviderConnection.id == rows.connection_id)
                           .values(config=null()).execution_options(synchronize_session=False, autoflush=False))
    elif representation == "managed-env":
        rows.connection.config = {MANAGED_CONNECTION_MARKER: {"enabled": True}}
    elif representation == "foreign-vault":
        owner = User(name="Foreign synthetic owner", email="r15-foreign@example.test", rol="admin", password_hash="synthetic")
        db.session.add(owner); db.session.flush()
        foreign = TenantProfile(slug="r15-foreign", nombre="Foreign", tipo="municipio", municipio_id=owner.id)
        db.session.add(foreign); db.session.flush()
        connection = ProviderConnection(tenant_id=foreign.id, provider="twilio", channel="whatsapp",
                                        environment="production", external_account_id=OTHER_ACCOUNT, status="needs_setup")
        db.session.add(connection); db.session.flush(); _install_envelope(connection)
    db.session.commit()
    before = copy.deepcopy(tuple(_database_projection(rows.connection_id)))
    config = _complete_whatsapp_config(rows, store_ready=False)
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, config)
    assert tech["setup_health"]["status"] == "configuration_complete"
    assert tech["setup_health"]["recommended_next_action"] == "await_live_test_support"
    assert tech["setup_health"]["blockers"] == []
    declared = tech["operation_availability"]["operations"]
    assert all(declared[name]["can_execute"] is True for name in ("register_sender", "poll_sender_status", "prepare_voice"))
    assert declared["live_whatsapp_message"]["can_execute"] is False
    assert declared["live_whatsapp_message"]["reason_code"] == "execution_not_implemented"
    assert declared["create_subaccount"]["can_execute"] is False
    # Compare the declared environment path with the existing executors using
    # synthetic responses; this checks compatibility, never actual delivery.
    calls = []
    def response(**kwargs):
        calls.append(kwargs["url"])
        if "/Applications" in kwargs["url"]:
            return {"sid": "AP-synthetic-voice"}
        return {"sid": state["sender_sid"], "status": "ONLINE", "sender_id": state["sender_id"]}
    for name in ("_twilio_post_form", "_twilio_post_json", "_twilio_get_json"):
        monkeypatch.setattr(runtime, name, response)
    assert runtime.register_whatsapp_sender(rows.tenant, {}, config)["ok"] is True
    assert runtime.poll_whatsapp_sender_status(rows.tenant, config)["ok"] is True
    assert runtime.provision_twilio_voice_application(rows.tenant, {}, config)["ok"] is True
    assert len(calls) == 5
    assert tuple(_database_projection(rows.connection_id)) == before
    assert AuditEvent.query.count() == 0


@pytest.mark.parametrize("sender_status", ["registered", "ready", "not_ready", "disconnected", "inactive"])
def test_configuration_sender_presence_never_counts_as_online(credential_db, sender_status):
    rows = credential_db
    _complete_whatsapp_state(rows, sender_status=sender_status)
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, _complete_whatsapp_config(rows))
    online = next(item for item in tech["operator_checklist"] if item["id"] == "sender_online")
    assert online["done"] is False
    assert tech["setup_health"]["status"] == "action_required"
    assert tech["setup_health"]["recommended_next_action"] == "poll_sender_status"
    assert next(item for item in tech["smoke_playbook"]["tests"] if item["id"] == "live_whatsapp_message")["can_execute"] is False


@pytest.mark.parametrize("missing", ["R15_SYNTHETIC_TENANT_TOKEN", "sender_sid", "live-enabled"])
def test_complete_configuration_does_not_advertise_unavailable_channel_poll(credential_db, missing):
    rows = credential_db
    state = _complete_whatsapp_state(rows)
    config = _complete_whatsapp_config(rows)
    if missing == "sender_sid":
        rows.tenant.configuracion = {"twilio_tech_provider": {**state, "sender_sid": None}}
        db.session.commit()
    elif missing == "live-enabled":
        config["TWILIO_TECH_PROVIDER_LIVE_ENABLED"] = False
    else:
        config.pop(missing)
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, config)
    assert tech["setup_health"]["status"] == "action_required"
    assert tech["operation_availability"]["operations"]["poll_sender_status"]["can_execute"] is False
    assert next(item for item in tech["smoke_playbook"]["tests"] if item["id"] == "production_channel")["can_execute"] is False


def test_complete_legacy_provider_status_requires_real_test_support_instead_of_pilot(credential_db):
    rows = credential_db
    _complete_whatsapp_state(rows)
    status = provider_platform.build_whatsapp_provider_status(rows.tenant, _complete_whatsapp_config(rows))
    assert all(item["ok"] for item in status["readiness_checks"])
    assert status["next_action"] == "await_live_test_support"
    assert status["frontend_contract"]["primary_action"] == "await_live_test_support"
    assert status["operational_readiness"]["status"] == "configuration_complete"
    assert status["operational_readiness"]["delivery_accepted"] is False
    assert status["operation_availability"]["operations"]["live_whatsapp_message"]["can_execute"] is False


@pytest.mark.parametrize("reported_ready", ["false", "true", 1])
def test_template_readiness_requires_exact_true_even_with_complete_ids(credential_db, reported_ready):
    rows = credential_db
    state = _complete_whatsapp_state(rows)
    rows.tenant.configuracion = {"twilio_tech_provider": {**state, "templates_ready": reported_ready}}
    db.session.commit()
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, _complete_whatsapp_config(rows))
    item = next(item for item in tech["operator_checklist"] if item["id"] == "templates_webviews")
    assert item["done"] is False
    assert tech["setup_health"]["status"] == "in_progress"
    assert tech["setup_health"]["recommended_next_action"] == "review_templates_and_webviews"


@pytest.mark.parametrize("field", ["requested_phone_number", "waba_id", "messaging_service_sid"])
def test_registration_availability_matches_missing_or_whitespace_executor_prerequisites(credential_db, monkeypatch, field):
    rows = credential_db
    state = _complete_whatsapp_state(rows)
    rows.tenant.configuracion = {"twilio_tech_provider": {**state, field: "   "}}
    db.session.commit()
    config = _complete_whatsapp_config(rows)
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, config)
    assert tech["operation_availability"]["operations"]["register_sender"]["can_execute"] is False
    def forbidden(*_args, **_kwargs):
        pytest.fail("Missing prerequisite cannot call provider")
    for name in ("_twilio_get_json", "_twilio_post_form", "_twilio_post_json"):
        monkeypatch.setattr(runtime, name, forbidden)
    executed = runtime.register_whatsapp_sender(rows.tenant, {}, config)
    assert executed["ok"] is False
    assert executed["reason_code"] == "sender_registration_prerequisites_missing"


@pytest.mark.parametrize("sender_case", ["registered", "missing-sender"])
@pytest.mark.parametrize("unavailable", ["live-disabled", "token-absent"])
def test_blocked_registration_or_poll_never_recommended_by_either_status_contract(credential_db, sender_case, unavailable):
    rows = credential_db
    state = _complete_whatsapp_state(rows, sender_status="registered")
    if sender_case == "missing-sender":
        state = {**state, "sender_sid": None, "sender_id": None,
                 "requested_phone_number": None, "sender_status": None}
        rows.tenant.configuracion = {"twilio_tech_provider": state}
        db.session.commit()
    config = _complete_whatsapp_config(rows)
    if unavailable == "live-disabled":
        config["TWILIO_TECH_PROVIDER_LIVE_ENABLED"] = False
    else:
        config.pop("R15_SYNTHETIC_TENANT_TOKEN")
    tech = runtime.build_twilio_tech_provider_contract(rows.tenant, config)
    provider = provider_platform.build_whatsapp_provider_status(rows.tenant, config)
    assert tech["setup_health"]["recommended_next_action"] == "wait_for_platform_activation"
    assert tech["frontend_contract"]["primary_action"] == "wait_for_platform_activation"
    assert provider["next_action"] == provider["frontend_contract"]["primary_action"] == "wait_for_platform_activation"
    operation = "register_sender" if sender_case == "missing-sender" else "poll_sender_status"
    assert tech["operation_availability"] == provider["operation_availability"]
    assert tech["operation_availability"]["operations"][operation]["can_execute"] is False
    assert tech["setup_health"]["blockers"][0]["code"] == provider["operational_readiness"]["blockers"][0]["code"]
    assert tech["setup_health"]["status"] == "action_required"


def test_managed_flip_between_inventory_and_locked_lookup_rejects_before_writes(credential_db):
    rows = credential_db
    marker = {MANAGED_CONNECTION_MARKER: {"enabled": True}}
    seen = []
    effect_session = db.session()
    def flip_before_locked_query(state):
        if not state.is_select:
            return
        descriptions = getattr(state.statement, "column_descriptions", ())
        if not any(item.get("entity") is ProviderConnection for item in descriptions):
            return
        seen.append(state.statement._for_update_arg is not None)
        if len(seen) == 2:
            assert seen == [False, True]
            effect_session.execute(
                update(ProviderConnection).where(ProviderConnection.id == rows.connection_id)
                .values(config=marker).execution_options(synchronize_session=False, autoflush=False)
            )
    event.listen(effect_session, "do_orm_execute", flip_before_locked_query)
    try:
        with pytest.raises(provider_platform.ManagedProviderConnectionStateError):
            provider_platform._get_or_create_connection(rows.tenant, _sync_config(rows))
    finally:
        event.remove(effect_session, "do_orm_execute", flip_before_locked_query)
    assert seen == [False, True]
    actual = _database_projection(rows.connection_id)
    assert actual.config == marker and actual.status == "needs_setup"
    assert actual.credentials_ref == "env:synthetic_legacy"
    assert AuditEvent.query.count() == 0 and ProviderSender.query.count() == 0


@pytest.mark.skipif(not POSTGRES_OPT_IN, reason="Explicit isolated loopback PostgreSQL service required")
def test_postgres_two_sessions_row_lock_serializes_rotation_and_rejects_losing_cas(credential_db):
    rows = credential_db
    assert rows.engine.dialect.name == "postgresql"
    _store(rows); db.session.commit()
    lock_attempted = threading.Event()
    completed = threading.Event()
    def contender():
        with rows.app.app_context(), Session(bind=rows.engine) as contender_session:
            def observe_lock(state):
                if state.is_select and getattr(state.statement, "_for_update_arg", None) is not None:
                    lock_attempted.set()
            event.listen(contender_session, "do_orm_execute", observe_lock)
            try:
                vault.store_tenant_twilio_token(
                    tenant_id=rows.tenant_id, connection_id=rows.connection_id,
                    account_sid=ACCOUNT, auth_token=TOKEN, expected_revision=1,
                    actor_user_id=rows.actor_id, app_config=rows.app.config,
                    session=contender_session,
                )
                contender_session.commit()
                return "unexpected_second_winner"
            except vault.ProviderCredentialError as error:
                contender_session.rollback()
                return str(error)
            finally:
                event.remove(contender_session, "do_orm_execute", observe_lock)
                completed.set()
    with Session(bind=rows.engine) as winner_session, ThreadPoolExecutor(max_workers=1) as executor:
        winner_session.execute(select(ProviderConnection).where(ProviderConnection.id == rows.connection_id)
                               .with_for_update()).scalar_one()
        future = executor.submit(contender)
        try:
            assert lock_attempted.wait(5), "Contender never attempted its row lock"
            assert not completed.wait(0.1), "Contender completed while another session held the row"
            assert vault.store_tenant_twilio_token(
                tenant_id=rows.tenant_id, connection_id=rows.connection_id,
                account_sid=ACCOUNT, auth_token=ROTATED_TOKEN, expected_revision=1,
                actor_user_id=rows.actor_id, app_config=rows.app.config,
                session=winner_session,
            ) == 2
            winner_session.commit()
        finally:
            # Also release a held lock if an assertion fails before commit.
            winner_session.rollback()
        assert future.result(timeout=10) == "provider_credential_revision_conflict"
    db.session.expire_all()
    assert vault.open_token(connection=db.session.get(ProviderConnection, rows.connection_id),
                            tenant_id=rows.tenant_id, app_config=rows.app.config).auth_token == ROTATED_TOKEN
    assert AuditEvent.query.count() == 2
