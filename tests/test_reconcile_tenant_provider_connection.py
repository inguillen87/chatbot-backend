import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import ProviderConnection, ProviderSender, TenantProfile, User, db
from scripts import reconcile_tenant_provider_connection as reconcile


ACCOUNT_SID = "AC0123456789abcdef0123456789abcdef"
OTHER_ACCOUNT_SID = "ACabcdef0123456789abcdef0123456789"
DATABASE_IDENTITY = "a" * 64
ACCOUNT_ENV = "JUNIN_TWILIO_ACCOUNT_SID"
CREDENTIAL_ENV = "JUNIN_TWILIO_AUTH_TOKEN"


def _seed_tenant(owner_user, *, active=True):
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=active,
    )
    db.session.add(tenant)
    db.session.commit()
    return tenant


def _request(*, evidence=False, environ=None):
    source = {ACCOUNT_ENV: ACCOUNT_SID} if environ is None else environ
    return reconcile.build_request(
        tenant_slug="junin",
        database_identity_sha256=DATABASE_IDENTITY,
        external_account_environment_variable=ACCOUNT_ENV,
        credentials_environment_variable=CREDENTIAL_ENV,
        provider_read_only_evidence_id=(
            "twilio-read-only-20260829-001" if evidence else None
        ),
        cutover_window_evidence_id=(
            "cutover-window-20260829-001" if evidence else None
        ),
        environ=source,
    )


def test_default_dry_run_plans_create_without_writes_and_redacts(
    client,
    owner_user,
):
    tenant = _seed_tenant(owner_user)

    result = reconcile.reconcile_tenant_provider_connection(
        db.session,
        _request(),
    )

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert result["writes_performed"] is False
    assert result["plan"]["proposed"]["action"] == "create"
    assert (
        result["plan"]["proposed"]["status"]
        == "pending_provider_verification"
    )
    assert result["plan"]["proposed"]["requires_provider_verification"] is True
    assert result["plan"]["scope"] == {
        "provider": "twilio",
        "channel": "whatsapp",
        "environment": "production",
    }
    assert result["plan"]["proposed"]["external_account"]["last4"] == "cdef"
    assert ProviderConnection.query.count() == 0
    db.session.refresh(tenant)

    rendered = json.dumps(result, sort_keys=True)
    assert ACCOUNT_SID not in rendered
    assert CREDENTIAL_ENV not in rendered
    assert f"env:{CREDENTIAL_ENV}" not in rendered
    assert "provider_calls_performed" in rendered
    assert result["plan"]["provider_calls_performed"] is False
    assert result["plan"]["messages_sent"] is False


def test_credentials_secret_is_never_read_or_rendered(client, owner_user):
    _seed_tenant(owner_user)

    class GuardedEnvironment(dict):
        def get(self, key, default=None):
            if key == CREDENTIAL_ENV:
                raise AssertionError("credential secret must not be read")
            return super().get(key, default)

    source = GuardedEnvironment(
        {
            ACCOUNT_ENV: ACCOUNT_SID,
            CREDENTIAL_ENV: "super-secret-token-that-must-not-be-read",
        }
    )
    result = reconcile.reconcile_tenant_provider_connection(
        db.session,
        _request(environ=source),
    )

    rendered = json.dumps(result, sort_keys=True)
    assert "super-secret-token-that-must-not-be-read" not in rendered
    assert CREDENTIAL_ENV not in rendered


def test_mocked_apply_is_exact_atomic_and_idempotent(
    client,
    owner_user,
    monkeypatch,
):
    tenant = _seed_tenant(owner_user)
    request = _request(evidence=True)
    dry_run = reconcile.reconcile_tenant_provider_connection(db.session, request)
    monkeypatch.setattr(
        reconcile,
        "acquire_postgres_advisory_lock",
        lambda *_args: None,
    )

    applied = reconcile.reconcile_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=dry_run["plan_sha256"],
    )

    assert applied["status"] == "applied"
    assert applied["writes_performed"] is True
    created = ProviderConnection.query.one()
    assert created.tenant_id == tenant.id
    assert created.provider == "twilio"
    assert created.channel == "whatsapp"
    assert created.environment == "production"
    assert created.status == "pending_provider_verification"
    assert created.external_account_id == ACCOUNT_SID
    assert created.credentials_ref == f"env:{CREDENTIAL_ENV}"

    second_plan = reconcile.reconcile_tenant_provider_connection(
        db.session,
        request,
    )
    assert second_plan["plan"]["proposed"]["action"] == "noop"
    second_apply = reconcile.reconcile_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=second_plan["plan_sha256"],
    )
    assert second_apply["status"] == "already_reconciled"
    assert second_apply["writes_performed"] is False
    assert ProviderConnection.query.count() == 1


def test_existing_incomplete_connection_is_repaired_without_rebinding(
    client,
    owner_user,
    monkeypatch,
):
    tenant = _seed_tenant(owner_user)
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="needs_setup",
    )
    db.session.add(connection)
    db.session.commit()
    request = _request(evidence=True)
    plan = reconcile.reconcile_tenant_provider_connection(db.session, request)

    assert plan["plan"]["proposed"]["action"] == "update"
    monkeypatch.setattr(
        reconcile,
        "acquire_postgres_advisory_lock",
        lambda *_args: None,
    )
    reconcile.reconcile_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=plan["plan_sha256"],
    )

    db.session.refresh(connection)
    assert connection.external_account_id == ACCOUNT_SID
    assert connection.credentials_ref == f"env:{CREDENTIAL_ENV}"
    assert connection.status == "pending_provider_verification"


def test_preexisting_online_connection_with_incomplete_authority_is_downgraded(
    client,
    owner_user,
    monkeypatch,
):
    tenant = _seed_tenant(owner_user)
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        external_account_id=None,
        credentials_ref=None,
    )
    db.session.add(connection)
    db.session.commit()
    request = _request(evidence=True)
    plan = reconcile.reconcile_tenant_provider_connection(db.session, request)
    assert plan["plan"]["proposed"]["status"] == (
        "pending_provider_verification"
    )
    monkeypatch.setattr(reconcile, "acquire_postgres_advisory_lock", lambda *_: None)
    reconcile.reconcile_tenant_provider_connection(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=plan["plan_sha256"],
    )
    db.session.refresh(connection)
    assert connection.status == "pending_provider_verification"


@pytest.mark.parametrize(
    "marker",
    [
        {
            "contract_version": reconcile.CONTRACT_VERSION,
            "enabled": True,
            "promotion_required": True,
        },
        {
            "contract_version": reconcile.CONTRACT_VERSION,
            "enabled": True,
            "promotion_required": False,
        },
        {
            "contract_version": reconcile.CONTRACT_VERSION,
            "enabled": True,
            "promotion_required": False,
            "provider_snapshot_sha256": "a" * 64,
            "credential_attestation_sha256": "b" * 64,
        },
    ],
)
def test_external_online_flip_without_complete_promotion_evidence_is_not_preserved(
    client,
    owner_user,
    marker,
):
    tenant = _seed_tenant(owner_user)
    db.session.add(
        ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="online",
            external_account_id=ACCOUNT_SID,
            credentials_ref=f"env:{CREDENTIAL_ENV}",
            config={reconcile.MANAGEMENT_MARKER: marker},
        )
    )
    db.session.commit()
    result = reconcile.reconcile_tenant_provider_connection(
        db.session,
        _request(evidence=True),
    )
    assert result["plan"]["proposed"]["status"] == (
        "pending_provider_verification"
    )


def test_complete_promotion_marker_preserves_online_and_evidence_hashes(
    client,
    owner_user,
):
    tenant = _seed_tenant(owner_user)
    marker = {
        "contract_version": reconcile.CONTRACT_VERSION,
        "enabled": True,
        "promotion_required": False,
        "provider_snapshot_sha256": "a" * 64,
        "credential_attestation_sha256": "b" * 64,
        "destination_deployment_revision": "c" * 40,
    }
    db.session.add(
        ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="online",
            external_account_id=ACCOUNT_SID,
            credentials_ref=f"env:{CREDENTIAL_ENV}",
            config={reconcile.MANAGEMENT_MARKER: marker},
        )
    )
    db.session.commit()
    result = reconcile.reconcile_tenant_provider_connection(
        db.session,
        _request(evidence=True),
    )
    assert result["plan"]["proposed"]["status"] == "online"
    assert result["plan"]["proposed"]["managed_config_sha256"]


def test_reconcile_and_promotion_share_exact_lock_contract():
    from scripts import promote_tenant_provider_connection as promote
    from services.provider_connection_cutover_contract import advisory_lock_keys

    inputs = {
        "database_identity_sha256": DATABASE_IDENTITY,
        "tenant_slug": "junin",
        "external_account_id": ACCOUNT_SID,
    }
    expected = advisory_lock_keys(**inputs)
    assert reconcile.advisory_lock_keys(**inputs) == expected
    assert promote.advisory_lock_keys(**inputs) == expected


def test_apply_requires_provider_evidence_and_window_before_lock(
    client,
    owner_user,
):
    _seed_tenant(owner_user)
    request = _request(evidence=False)

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="provider_read_only_evidence_id_required",
    ):
        reconcile.reconcile_tenant_provider_connection(
            db.session,
            request,
            apply=True,
            approved_plan_sha256="a" * 64,
        )

    assert ProviderConnection.query.count() == 0


def test_apply_requires_independent_provider_and_window_evidence(
    client,
    owner_user,
):
    _seed_tenant(owner_user)
    request = reconcile.build_request(
        tenant_slug="junin",
        database_identity_sha256=DATABASE_IDENTITY,
        external_account_environment_variable=ACCOUNT_ENV,
        credentials_environment_variable=CREDENTIAL_ENV,
        provider_read_only_evidence_id="shared-evidence-20260829-001",
        cutover_window_evidence_id="shared-evidence-20260829-001",
        environ={ACCOUNT_ENV: ACCOUNT_SID},
    )

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="provider_and_window_evidence_must_differ",
    ):
        reconcile.reconcile_tenant_provider_connection(
            db.session,
            request,
            apply=True,
            approved_plan_sha256="a" * 64,
        )

    assert ProviderConnection.query.count() == 0


def test_apply_requires_postgresql_and_leaves_sqlite_unchanged(
    client,
    owner_user,
):
    _seed_tenant(owner_user)
    request = _request(evidence=True)
    plan = reconcile.reconcile_tenant_provider_connection(db.session, request)

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="apply_postgresql_required",
    ):
        reconcile.reconcile_tenant_provider_connection(
            db.session,
            request,
            apply=True,
            approved_plan_sha256=plan["plan_sha256"],
        )

    assert ProviderConnection.query.count() == 0


def test_approved_plan_mismatch_blocks_before_mutation(
    client,
    owner_user,
    monkeypatch,
):
    _seed_tenant(owner_user)
    request = _request(evidence=True)
    monkeypatch.setattr(
        reconcile,
        "acquire_postgres_advisory_lock",
        lambda *_args: None,
    )

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="approved_plan_sha256_mismatch",
    ):
        reconcile.reconcile_tenant_provider_connection(
            db.session,
            request,
            apply=True,
            approved_plan_sha256="f" * 64,
        )

    assert ProviderConnection.query.count() == 0


def test_cross_tenant_account_claim_blocks(client, owner_user):
    _seed_tenant(owner_user)
    other_owner = User(
        name="Other owner",
        email="provider-account-owner@example.test",
        rol="admin",
    )
    other_owner.set_password("test-only")
    db.session.add(other_owner)
    db.session.flush()
    other_tenant = TenantProfile(
        slug="other-tenant",
        nombre="Other tenant",
        tipo="municipio",
        municipio_id=other_owner.id,
        is_active=True,
    )
    db.session.add(other_tenant)
    db.session.flush()
    db.session.add(
        ProviderConnection(
            tenant_id=other_tenant.id,
            provider="twilio",
            channel="whatsapp",
            environment="production",
            status="online",
            external_account_id=ACCOUNT_SID,
            credentials_ref="env:OTHER_TWILIO_AUTH_TOKEN",
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="external_account_claimed_by_other_tenant",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())


def test_target_account_in_other_scope_blocks(client, owner_user):
    tenant = _seed_tenant(owner_user)
    db.session.add(
        ProviderConnection(
            tenant_id=tenant.id,
            provider="twilio",
            channel="sms",
            environment="production",
            status="online",
            external_account_id=ACCOUNT_SID,
            credentials_ref=f"env:{CREDENTIAL_ENV}",
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="external_account_claimed_by_other_connection_scope",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())


def test_more_than_one_normalized_target_connection_blocks(client, owner_user):
    tenant = _seed_tenant(owner_user)
    db.session.add_all(
        [
            ProviderConnection(
                tenant_id=tenant.id,
                provider="twilio",
                channel="whatsapp",
                environment="production",
                status="needs_setup",
            ),
            ProviderConnection(
                tenant_id=tenant.id,
                provider="TWILIO",
                channel="WHATSAPP",
                environment="PRODUCTION",
                status="needs_setup",
            ),
        ]
    )
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="provider_connection_more_than_one",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())


def test_existing_nonempty_account_or_credentials_cannot_be_silently_rebound(
    client,
    owner_user,
):
    tenant = _seed_tenant(owner_user)
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="needs_setup",
        external_account_id=OTHER_ACCOUNT_SID,
        credentials_ref=f"env:{CREDENTIAL_ENV}",
    )
    db.session.add(connection)
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="provider_connection_external_account_mismatch",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())

    connection.external_account_id = ACCOUNT_SID
    connection.credentials_ref = "env:DIFFERENT_TWILIO_AUTH_TOKEN"
    db.session.commit()
    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="provider_connection_credentials_ref_mismatch",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())


def test_tenant_must_be_exact_active_and_have_unique_owner(client, owner_user):
    tenant = TenantProfile(
        slug="Junin",
        nombre="Wrong case",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="tenant_slug_exact_mismatch",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())

    tenant.slug = "junin"
    tenant.is_active = False
    db.session.commit()
    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="tenant_inactive",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())

    tenant.is_active = True
    db.session.commit()
    result = reconcile.reconcile_tenant_provider_connection(db.session, _request())
    assert result["status"] == "dry_run"

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="tenant_owner_missing_or_ambiguous",
    ):
        reconcile._tenant_owner_id(
            SimpleNamespace(municipio_id=owner_user.id, pyme_id=owner_user.id + 1)
        )


def test_other_active_tenant_cannot_share_owner(client, owner_user):
    _seed_tenant(owner_user)
    db.session.add(
        TenantProfile(
            slug="shared-owner",
            nombre="Shared owner",
            tipo="municipio",
            municipio_id=owner_user.id,
            is_active=True,
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="tenant_owner_claimed_by_other_active_tenant",
    ):
        reconcile.reconcile_tenant_provider_connection(db.session, _request())


def test_account_and_environment_variable_validation_is_fail_closed():
    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="external_account_id_invalid",
    ):
        reconcile.build_request(
            tenant_slug="junin",
            database_identity_sha256=DATABASE_IDENTITY,
            external_account_environment_variable=ACCOUNT_ENV,
            credentials_environment_variable=CREDENTIAL_ENV,
            environ={ACCOUNT_ENV: "AC-not-a-real-account-sid"},
        )

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="credentials_environment_variable_name_invalid",
    ):
        reconcile.build_request(
            tenant_slug="junin",
            database_identity_sha256=DATABASE_IDENTITY,
            external_account_environment_variable=ACCOUNT_ENV,
            credentials_environment_variable="not-safe",
            environ={ACCOUNT_ENV: ACCOUNT_SID},
        )

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="credentials_environment_variable_must_differ_from_account",
    ):
        reconcile.build_request(
            tenant_slug="junin",
            database_identity_sha256=DATABASE_IDENTITY,
            external_account_environment_variable=ACCOUNT_ENV,
            credentials_environment_variable=ACCOUNT_ENV,
            environ={ACCOUNT_ENV: ACCOUNT_SID},
        )


def test_postgres_advisory_lock_is_transaction_scoped_and_redacted():
    request = _request(evidence=True)
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        get_isolation_level=lambda: "SERIALIZABLE",
    )

    reconcile.acquire_postgres_advisory_lock(session, request)

    statement, parameters = session.execute.call_args.args
    assert session.execute.call_count == 2
    assert "pg_try_advisory_xact_lock" in str(statement)
    assert isinstance(parameters["lock_key"], int)
    assert ACCOUNT_SID not in repr(session.execute.call_args)


def test_postgres_apply_rejects_non_serializable_transaction():
    request = _request(evidence=True)
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        get_isolation_level=lambda: "READ COMMITTED",
    )

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="apply_serializable_transaction_required",
    ):
        reconcile.acquire_postgres_advisory_lock(session, request)

    session.execute.assert_not_called()


def test_contended_advisory_lock_requires_fresh_transaction_retry():
    request = _request(evidence=True)
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        get_isolation_level=lambda: "SERIALIZABLE",
    )
    session.execute.return_value.scalar_one.return_value = False

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="advisory_lock_contended_retry_required",
    ):
        reconcile.acquire_postgres_advisory_lock(session, request)


def test_database_identity_requires_postgres_tls_and_is_redacted():
    fingerprint, summary = reconcile._database_identity(
        "postgresql+psycopg://private-user:private-password@db.example.test/app"
        "?sslmode=require"
    )
    rendered = json.dumps(summary, sort_keys=True)

    assert fingerprint == summary["identity_sha256"]
    assert len(fingerprint) == 64
    assert summary["tls"] is True
    assert "private-user" not in rendered
    assert "private-password" not in rendered
    assert "db.example.test" not in rendered

    with pytest.raises(
        reconcile.ProviderConnectionReconciliationError,
        match="database_tls_required",
    ):
        reconcile._database_identity(
            "postgresql+psycopg://user:password@db.example.test/app"
        )


def test_runtime_engine_url_forces_psycopg_v3_without_dsn_drift():
    runtime_url = reconcile._psycopg_engine_url(
        "postgresql://private-user:private-password@db.example.test:5432/app"
        "?sslmode=require&channel_binding=require"
    )

    assert runtime_url.drivername == "postgresql+psycopg"
    assert runtime_url.username == "private-user"
    assert runtime_url.password == "private-password"
    assert runtime_url.host == "db.example.test"
    assert runtime_url.port == 5432
    assert runtime_url.database == "app"
    assert runtime_url.query["sslmode"] == "require"
    assert runtime_url.query["channel_binding"] == "require"
