import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from models import (
    ProviderConnection,
    ProviderSender,
    TenantProfile,
    User,
    WhatsappNumero,
    db,
)
from scripts import reconcile_tenant_whatsapp_sender as reconcile


OFFICIAL_PHONE = "+17432643718"
PROFILE_PHONE = "+5492615550101"
SANDBOX_PHONE = "+14155238886"
SENDER_SID = "XE1234567890ABCDEF"
MESSAGING_SERVICE_SID = "MG1234567890ABCDEF"
DATABASE_IDENTITY = "a" * 64


def _seed_target(
    owner_user,
    *,
    active=True,
    credentials_ref="env:twilio_junin",
    profile_sender=SANDBOX_PHONE,
):
    owner_user.telefono = PROFILE_PHONE
    tenant = TenantProfile(
        slug="junin",
        nombre="Municipalidad de Junín",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=active,
        whatsapp_sender_id=(f"whatsapp:{profile_sender}" if profile_sender else None),
        configuracion={},
    )
    db.session.add(tenant)
    db.session.flush()
    legacy = WhatsappNumero(
        numero_whatsapp=OFFICIAL_PHONE,
        user_id=owner_user.id,
        is_active=True,
    )
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="subaccount_created",
        credentials_ref=credentials_ref,
        external_account_id="AC-not-printed",
    )
    db.session.add_all([legacy, connection])
    db.session.commit()
    return tenant, connection, legacy


def _request(*, evidence=False, external_metadata=True):
    environ = {}
    sender_env = None
    service_env = None
    if external_metadata:
        sender_env = "JUNIN_TWILIO_SENDER_SID"
        service_env = "JUNIN_TWILIO_MESSAGING_SERVICE_SID"
        environ[sender_env] = SENDER_SID
        environ[service_env] = MESSAGING_SERVICE_SID
    return reconcile.build_request(
        tenant_slug="junin",
        official_phone=OFFICIAL_PHONE,
        provider="twilio",
        environment="production",
        database_identity_sha256=DATABASE_IDENTITY,
        sender_sid_environment_variable=sender_env,
        messaging_service_sid_environment_variable=service_env,
        twilio_read_only_evidence_id=("twilio-ro-20260829-001" if evidence else None),
        cutover_window_evidence_id=("cutover-window-20260829-001" if evidence else None),
        environ=environ,
    )


def test_default_dry_run_is_redacted_and_performs_no_writes(client, owner_user):
    tenant, connection, _ = _seed_target(owner_user)
    alias_owner = User(
        name="Alias sin sender",
        email="alias-without-sender@example.test",
        rol="admin",
    )
    alias_owner.set_password("test-only")
    db.session.add(alias_owner)
    db.session.flush()
    db.session.add(
        TenantProfile(
            slug="municipio",
            nombre="Alias sin ownership del sender",
            tipo="municipio",
            municipio_id=alias_owner.id,
            is_active=True,
        )
    )
    db.session.commit()
    result = reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())

    assert result["status"] == "dry_run"
    assert result["dry_run"] is True
    assert result["writes_performed"] is False
    assert result["plan"]["proposed"]["sender_action"] == "create"
    assert result["plan"]["proposed"]["profile_action"] == "align"
    assert result["plan"]["connection"]["id"] == connection.id
    assert result["plan"]["connection"]["credentials_ref_present"] is True
    assert len(result["plan"]["connection"]["credentials_ref_sha256"]) == 64
    assert result["plan"]["connection"]["external_account"]["configured"] is True
    assert result["plan"]["connection"]["status"] == "subaccount_created"
    assert result["plan"]["official_phone"]["last4"] == "3718"
    assert ProviderSender.query.count() == 0
    db.session.refresh(tenant)
    assert tenant.whatsapp_sender_id == f"whatsapp:{SANDBOX_PHONE}"

    rendered = json.dumps(result, sort_keys=True)
    assert OFFICIAL_PHONE not in rendered
    assert PROFILE_PHONE not in rendered
    assert SANDBOX_PHONE not in rendered
    assert SENDER_SID not in rendered
    assert MESSAGING_SERVICE_SID not in rendered
    assert "AC-not-printed" not in rendered
    assert "env:twilio_junin" not in rendered


def test_apply_requires_postgresql_and_leaves_sqlite_unchanged(client, owner_user):
    tenant, _, _ = _seed_target(owner_user)
    request = _request(evidence=True)
    dry_run = reconcile.reconcile_tenant_whatsapp_sender(db.session, request)

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="apply_postgresql_required",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(
            db.session,
            request,
            apply=True,
            approved_plan_sha256=dry_run["plan_sha256"],
        )

    assert ProviderSender.query.count() == 0
    db.session.refresh(tenant)
    assert tenant.whatsapp_sender_id == f"whatsapp:{SANDBOX_PHONE}"


def test_apply_requires_twilio_and_cutover_evidence_before_lock(client, owner_user):
    _seed_target(owner_user)
    request = _request(evidence=False)

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="twilio_read_only_evidence_id_required",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(
            db.session,
            request,
            apply=True,
            approved_plan_sha256="a" * 64,
        )

    assert ProviderSender.query.count() == 0


def test_mocked_apply_is_atomic_in_scope_and_idempotent(
    client,
    owner_user,
    monkeypatch,
):
    tenant, connection, _ = _seed_target(owner_user)
    unrelated_owner = User(
        name="Otro municipio",
        email="other-sender-owner@example.test",
        rol="admin",
        tipo_chat="municipio",
    )
    unrelated_owner.set_password("test-only")
    db.session.add(unrelated_owner)
    db.session.flush()
    unrelated_tenant = TenantProfile(
        slug="other-sender-tenant",
        nombre="Otro municipio",
        tipo="municipio",
        municipio_id=unrelated_owner.id,
        is_active=True,
        whatsapp_sender_id="whatsapp:+15550009999",
    )
    db.session.add(unrelated_tenant)
    db.session.flush()
    unrelated_connection = ProviderConnection(
        tenant_id=unrelated_tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        credentials_ref="env:other_twilio",
    )
    db.session.add(unrelated_connection)
    db.session.flush()
    unrelated_sender = ProviderSender(
        tenant_id=unrelated_tenant.id,
        provider_connection_id=unrelated_connection.id,
        channel="whatsapp",
        sender_type="whatsapp_business",
        phone_number="+15550009999",
        sender_id="whatsapp:+15550009999",
        status="active",
    )
    db.session.add(unrelated_sender)
    db.session.commit()

    request = _request(evidence=True)
    first_plan = reconcile.reconcile_tenant_whatsapp_sender(db.session, request)
    monkeypatch.setattr(reconcile, "acquire_postgres_advisory_lock", lambda *_args: None)
    applied = reconcile.reconcile_tenant_whatsapp_sender(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=first_plan["plan_sha256"],
    )

    assert applied["status"] == "applied"
    assert applied["writes_performed"] is True
    created = ProviderSender.query.filter_by(tenant_id=tenant.id).one()
    assert created.provider_connection_id == connection.id
    assert created.phone_number == OFFICIAL_PHONE
    assert created.sender_id == f"whatsapp:{OFFICIAL_PHONE}"
    assert created.sender_sid == SENDER_SID
    assert created.messaging_service_sid == MESSAGING_SERVICE_SID
    assert created.status == "registered"
    db.session.refresh(tenant)
    assert tenant.whatsapp_sender_id == f"whatsapp:{OFFICIAL_PHONE}"

    db.session.refresh(unrelated_tenant)
    db.session.refresh(unrelated_sender)
    assert unrelated_tenant.whatsapp_sender_id == "whatsapp:+15550009999"
    assert unrelated_sender.phone_number == "+15550009999"

    second_plan = reconcile.reconcile_tenant_whatsapp_sender(db.session, request)
    assert second_plan["plan"]["proposed"]["sender_action"] == "noop"
    assert second_plan["plan"]["proposed"]["profile_action"] == "noop"
    second_apply = reconcile.reconcile_tenant_whatsapp_sender(
        db.session,
        request,
        apply=True,
        approved_plan_sha256=second_plan["plan_sha256"],
    )
    assert second_apply["status"] == "already_reconciled"
    assert second_apply["writes_performed"] is False
    assert ProviderSender.query.filter_by(tenant_id=tenant.id).count() == 1


def test_approved_plan_mismatch_blocks_before_mutation(
    client,
    owner_user,
    monkeypatch,
):
    tenant, _, _ = _seed_target(owner_user)
    request = _request(evidence=True)
    monkeypatch.setattr(reconcile, "acquire_postgres_advisory_lock", lambda *_args: None)

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="approved_plan_sha256_mismatch",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(
            db.session,
            request,
            apply=True,
            approved_plan_sha256="f" * 64,
        )

    assert ProviderSender.query.count() == 0
    db.session.refresh(tenant)
    assert tenant.whatsapp_sender_id == f"whatsapp:{SANDBOX_PHONE}"


def test_other_tenant_phone_claim_blocks_reconciliation(client, owner_user):
    _seed_target(owner_user)
    other_owner = User(
        name="Conflicting owner",
        email="conflicting-owner@example.test",
        rol="admin",
    )
    other_owner.set_password("test-only")
    db.session.add(other_owner)
    db.session.flush()
    db.session.add(
        TenantProfile(
            slug="conflicting-tenant",
            nombre="Conflicting tenant",
            tipo="municipio",
            municipio_id=other_owner.id,
            is_active=True,
            whatsapp_sender_id=f"whatsapp:{OFFICIAL_PHONE}",
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="official_phone_claimed_by_other_tenant",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())


def test_other_active_tenant_cannot_share_legacy_owner(client, owner_user):
    _seed_target(owner_user)
    db.session.add(
        TenantProfile(
            slug="shared-owner-conflict",
            nombre="Shared owner conflict",
            tipo="municipio",
            municipio_id=owner_user.id,
            is_active=True,
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="tenant_owner_claimed_by_other_active_tenant",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())


def test_other_tenant_provider_sender_claim_blocks_reconciliation(client, owner_user):
    _seed_target(owner_user)
    other_owner = User(
        name="Provider conflict owner",
        email="provider-conflict-owner@example.test",
        rol="admin",
    )
    other_owner.set_password("test-only")
    db.session.add(other_owner)
    db.session.flush()
    other_tenant = TenantProfile(
        slug="provider-conflict-tenant",
        nombre="Provider conflict tenant",
        tipo="municipio",
        municipio_id=other_owner.id,
        is_active=True,
    )
    db.session.add(other_tenant)
    db.session.flush()
    other_connection = ProviderConnection(
        tenant_id=other_tenant.id,
        provider="twilio",
        channel="whatsapp",
        environment="production",
        status="online",
        credentials_ref="env:other_twilio",
    )
    db.session.add(other_connection)
    db.session.flush()
    db.session.add(
        ProviderSender(
            tenant_id=other_tenant.id,
            provider_connection_id=other_connection.id,
            channel="whatsapp",
            phone_number=OFFICIAL_PHONE,
            sender_id=f"whatsapp:{OFFICIAL_PHONE}",
            status="active",
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="official_phone_claimed_by_other_provider_sender",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())


def test_legacy_owner_and_credentials_are_required(client, owner_user):
    tenant, connection, legacy = _seed_target(owner_user)
    connection.credentials_ref = None
    db.session.commit()
    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="provider_connection_credentials_ref_missing",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())

    connection.credentials_ref = "env:twilio_junin"
    legacy.user_id = owner_user.id + 999
    db.session.commit()
    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="legacy_whatsapp_number_owner_conflict",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())
    assert tenant.whatsapp_sender_id == f"whatsapp:{SANDBOX_PHONE}"


def test_alternate_format_legacy_number_conflict_fails_closed(client, owner_user):
    _seed_target(owner_user)
    other_owner = User(
        name="Legacy conflict owner",
        email="legacy-conflict-owner@example.test",
        rol="admin",
    )
    other_owner.set_password("test-only")
    db.session.add(other_owner)
    db.session.flush()
    db.session.add(
        WhatsappNumero(
            numero_whatsapp="+1 (743) 264-3718",
            user_id=other_owner.id,
            is_active=True,
        )
    )
    db.session.commit()

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="legacy_whatsapp_number_exactly_one_required",
    ):
        reconcile.reconcile_tenant_whatsapp_sender(db.session, _request())


def test_external_metadata_must_come_from_named_environment_variables():
    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="sender_sid_environment_variable_missing",
    ):
        reconcile.build_request(
            tenant_slug="junin",
            official_phone=OFFICIAL_PHONE,
            provider="twilio",
            environment="production",
            database_identity_sha256=DATABASE_IDENTITY,
            sender_sid_environment_variable="JUNIN_TWILIO_SENDER_SID",
            environ={},
        )

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="sender_sid_invalid",
    ):
        reconcile.build_request(
            tenant_slug="junin",
            official_phone=OFFICIAL_PHONE,
            provider="twilio",
            environment="production",
            database_identity_sha256=DATABASE_IDENTITY,
            sender_sid_environment_variable="JUNIN_TWILIO_SENDER_SID",
            environ={"JUNIN_TWILIO_SENDER_SID": "not-a-twilio-sid"},
        )


def test_postgres_advisory_lock_is_transaction_scoped_and_redacted():
    request = _request(evidence=True, external_metadata=False)
    session = MagicMock()
    session.get_bind.return_value = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql")
    )

    reconcile.acquire_postgres_advisory_lock(session, request)

    statement, parameters = session.execute.call_args.args
    assert "pg_advisory_xact_lock" in str(statement)
    assert isinstance(parameters["lock_key"], int)
    assert OFFICIAL_PHONE not in repr(session.execute.call_args)


def test_database_identity_requires_postgres_tls_and_is_redacted():
    fingerprint, summary = reconcile._database_identity(
        "postgresql+psycopg://private-user:private-password@db.example.test/app?sslmode=require"
    )
    rendered = json.dumps(summary, sort_keys=True)

    assert fingerprint == summary["identity_sha256"]
    assert len(fingerprint) == 64
    assert summary["tls"] is True
    assert "private-user" not in rendered
    assert "private-password" not in rendered
    assert "db.example.test" not in rendered

    with pytest.raises(
        reconcile.SenderReconciliationError,
        match="database_tls_required",
    ):
        reconcile._database_identity(
            "postgresql+psycopg://user:password@db.example.test/app"
        )


def test_runtime_engine_url_forces_psycopg_v3_without_changing_dsn_fields():
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
