"""Offline HTTP/transaction proofs for the separate persistent rehearsal.

All accounts, secrets and addresses are synthetic. Functional SQLite tests
replace only the unavailable production runtime/epoch lease boundary; tokens,
AuthSession, membership, receipts, persistence and constraints remain real.
The production boundary itself is exercised separately without connecting.
"""
from contextlib import contextmanager
from datetime import timedelta
import json
import os
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from app import create_app
from config import TestingConfig
from database import db
from models import (AuditEvent, AuthSession, DemoSurveyParticipation, EncEncuesta,
                    EncRespuesta, SurveyResponseEffect, SurveyResponseReceipt,
                    TenantProfile, User)
from services import production_survey_rehearsal as service
from utils.auth_helpers import generar_token, auth_session_version


@pytest.fixture(scope="module")
def rehearsal_app(tmp_path_factory):
    path = (tmp_path_factory.mktemp("rehearsal") / "local.sqlite3").as_posix()
    config = type("RehearsalTestConfig", (TestingConfig,), {
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{path}",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"check_same_thread": False, "timeout": 10}},
        "SECRET_KEY": "synthetic-rehearsal-test-secret-never-a-live-credential",
        "CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED": False,
        "CUTOVER_WRITER_FENCE_ENABLED": False,
        "ENABLE_RUNTIME_SCHEMA_SYNC": False, "ENABLE_RUNTIME_TENANT_INIT": False,
        "PUBLIC_ENCUESTAS_RATE_LIMIT": 10000,
        "ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT": False,
    })
    return create_app(config)


def _headers(user, *, mfa=True):
    claims = {"tenant_id": user.tenant_id, "tenant_slug": user.tenant_slug}
    if user.rol == "super_admin":
        now = int(service._now().timestamp())
        claims.update(auth_provider="clerk", session_kind="clerk", sid="synthetic-sa-session",
                      clerk_sid="synthetic-sa-session", sv=auth_session_version(user))
        if mfa:
            claims["auth_assurance"] = {"version": "auth.assurance.v1", "source": "clerk_v2_fva",
                "status": "verified", "first_factor_verified_at": now, "second_factor_verified_at": now}
    token = generar_token(user.id, user.rol, user.tipo_chat, user.municipio_id, user.pyme_id, extra_claims=claims)
    return {"Authorization": "Bearer " + token}


@contextmanager
def _local_harness(rehearsal_app, monkeypatch):
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", "sa@rehearsal.test.invalid")
    decision = SimpleNamespace(allowed=True, enabled=True, epoch=6)
    lease = SimpleNamespace(decision=decision, revalidate=lambda config: decision)
    @contextmanager
    def local_lease(config):
        yield lease
    monkeypatch.setattr(service, "require_production_runtime", lambda: None)
    monkeypatch.setattr(service, "global_writer_authority_lease", local_lease)
    with rehearsal_app.app_context():
        db.create_all()
        def user(email, role="usuario", tenant=None):
            result = User(name="Cuenta sintética", email=email, rol=role, tipo_chat="municipio")
            result.set_password("synthetic-offline-password")
            if tenant is not None:
                result.tenant_id, result.tenant_slug = tenant.id, tenant.slug
            db.session.add(result)
            db.session.flush()
            return result
        owner = user("owner-a@rehearsal.test.invalid", "admin_municipio")
        foreign_owner = user("owner-b@rehearsal.test.invalid", "admin_municipio")
        tenant = TenantProfile(slug="rehearsal-a", nombre="Organización de prueba A", tipo="municipio",
                               plan="full", municipio_id=owner.id, is_active=True)
        foreign = TenantProfile(slug="rehearsal-b", nombre="Organización de prueba B", tipo="municipio",
                                plan="full", municipio_id=foreign_owner.id, is_active=True)
        db.session.add_all([tenant, foreign]); db.session.flush()
        owner.tenant_id, owner.tenant_slug = tenant.id, tenant.slug
        actor = user("a@rehearsal.test.invalid", tenant=tenant)
        second = user("a2@rehearsal.test.invalid", tenant=tenant)
        other = user("b@rehearsal.test.invalid", tenant=foreign)
        orphan = user("orphan@rehearsal.test.invalid")
        sa = user("sa@rehearsal.test.invalid", "super_admin")
        db.session.commit()
        harness = SimpleNamespace(app=rehearsal_app, client=rehearsal_app.test_client(), tenant=tenant,
            foreign=foreign, owner=owner, actor=actor, second=second, other=other, orphan=orphan, sa=sa, lease=lease)
        harness.headers = {name: _headers(getattr(harness, name)) for name in ("owner", "actor", "second", "other", "orphan", "sa")}
        harness.slug = tenant.slug
        try:
            yield harness
        finally:
            db.session.rollback(); db.session.remove()
            if db.engine.dialect.name == "sqlite":
                db.drop_all()


@pytest.fixture
def h(rehearsal_app, monkeypatch):
    with _local_harness(rehearsal_app, monkeypatch) as harness:
        yield harness


def _admin(slug):
    return f"/api/v2/tenants/{slug}/survey-rehearsals"


def _public(slug, run):
    return f"/api/v2/public/tenants/{slug}/survey-rehearsals/{run}"


def _create(h, key="create-intent-0001", *, slug=None, headers=None):
    response = h.client.post(_admin(slug or h.tenant.slug), json={},
        headers={**(headers or h.headers["sa"]), "Idempotency-Key": key})
    assert response.status_code in {200, 201}, response.get_json()
    return response.get_json()


def _vote(h, run, key="vote-intent-0001", *, actor="actor", option="yes", slug=None, client=None, **kwargs):
    return (client or h.client).post(_public(slug or h.tenant.slug, run) + "/respond",
        json={"submission_id": key, "option_id": option},
        headers={**h.headers[actor], "Idempotency-Key": key}, **kwargs)


def _ledger_counts():
    return (DemoSurveyParticipation.query.count(), AuditEvent.query.filter_by(event_type=service.ACCEPTED).count())


def test_signed_creation_metadata_is_separate_public_zero_ledger_no_official_records(h):
    data = _create(h)
    read = h.client.get(_public(h.tenant.slug, data["run_id"]))
    assert read.status_code == 200 and read.headers["Cache-Control"] == "no-store"
    body = read.get_json()
    assert body["question"] == service.QUESTION and body["metrics"]["total_responses"] == 0
    assert body["persisted"] is True and body["seeded_responses"] == 0
    assert body["official"] is False and body["result_certified"] is False
    assert body["unique_person_certified"] is False and body["refresh"]["socket_delivery_proven"] is False
    assert body["refresh"]["interval_ms"] == 5000
    assert body["ui"]["warning"] == service.WARNING and body["branding"]["display_name"] == h.tenant.nombre
    assert body["max_responses"] == 20 and _ledger_counts() == (0, 0)
    assert EncEncuesta.query.count() == EncRespuesta.query.count() == 0
    assert SurveyResponseReceipt.query.count() == SurveyResponseEffect.query.count() == 0
    assert h.tenant.jurisdiction_ref is None and h.tenant.jurisdiction_status == "unverified"
    serialized = json.dumps(body)
    assert not any(private in serialized for private in ("@rehearsal", "account_hmac", "request_digest", "actor_user_id"))


def test_create_exact_intent_replays_and_readonly_status_bind_original_key(h):
    first = _create(h)
    second = _create(h)
    status = h.client.get(_admin(h.tenant.slug) + "/status?submission_id=create-intent-0001", headers=h.headers["sa"])
    assert first["run_id"] == second["run_id"] == status.get_json()["run_id"]
    assert second["replayed"] is True and status.get_json()["submission_id"] == "create-intent-0001"
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 1
    missing = h.client.get(_admin(h.tenant.slug) + "/status?submission_id=missing-key-0001", headers=h.headers["sa"])
    assert missing.status_code == 404 and _ledger_counts() == (0, 0)


def test_admin_descriptor_and_normal_tenant_read_do_not_grant_create(h):
    read = h.client.get(_admin(h.tenant.slug), headers=h.headers["owner"])
    assert read.status_code == 200, read.get_json()
    assert read.get_json()["create_action"]["can_create"] is False
    denied = h.client.post(_admin(h.tenant.slug), json={}, headers={**h.headers["owner"], "Idempotency-Key": "deny-admin-0001"})
    assert denied.status_code == 403 and AuditEvent.query.count() == 0
    foreign = h.client.get(_admin(h.foreign.slug), headers=h.headers["owner"])
    assert foreign.status_code == 403
    sa = h.client.get(_admin(h.tenant.slug), headers=h.headers["sa"])
    assert sa.get_json()["create_action"]["can_create"] is True
    assert sa.get_json()["create_action"]["requires_strict_mfa"] is False


def test_sa_normal_live_session_creates_without_new_mfa_requirement_retired_session_denied(h):
    headers = _headers(h.sa, mfa=False)
    descriptor = h.client.get(_admin(h.slug), headers=headers).get_json()["create_action"]
    assert descriptor["can_create"] is True and descriptor["blocked_reason_code"] is None
    assert descriptor["requires_strict_mfa"] is False
    created = h.client.post(_admin(h.slug), json={}, headers={**headers, "Idempotency-Key": "normal-sa-key-0001"})
    assert created.status_code == 201, created.get_json()
    AuthSession.query.filter_by(actor_id=h.sa.id).update({"revoked_at": service._now()})
    db.session.commit()
    denied = h.client.post(_admin(h.slug), json={}, headers={**headers, "Idempotency-Key": "retired-sa-key-0001"})
    assert denied.status_code == 401
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 1


def test_same_account_new_browser_receipt_exact_replay_changed_body_and_newkey(h):
    run = _create(h)["run_id"]
    first = _vote(h, run)
    assert first.status_code == 201, json.dumps(first.get_json())
    ack = first.get_json()
    assert ack["receipt"]["submission_id"] == "vote-intent-0001"
    assert ack["receipt"]["verified_current_account"] is True and "request_digest" not in ack["receipt"]
    replay = _vote(h, run)
    assert replay.status_code == 200 and replay.get_json()["replayed"] is True
    assert _vote(h, run, option="no").status_code == 409
    assert _vote(h, run, "new-key-same-account").status_code == 409
    fresh_client = h.app.test_client()
    own = fresh_client.get(_public(h.tenant.slug, run) + "/respond/status", headers=h.headers["actor"])
    assert own.status_code == 200 and own.get_json()["participated"] is True
    assert own.get_json()["contract_version"] == "surveys.production_rehearsal.account_status.v1"
    exact = fresh_client.get(_public(h.tenant.slug, run) + "/respond/status?submission_id=vote-intent-0001", headers=h.headers["actor"])
    assert exact.status_code == 200 and exact.get_json()["receipt"] == ack["receipt"]
    assert _ledger_counts() == (1, 1)


def test_account_status_is_authoritative_absent_without_cache_and_scoped_to_current_account(h):
    run = _create(h)["run_id"]
    before = h.client.get(_public(h.tenant.slug, run) + "/respond/status", headers=h.headers["actor"])
    assert before.get_json()["participated"] is False
    _vote(h, run)
    other = h.client.get(_public(h.tenant.slug, run) + "/respond/status", headers=h.headers["second"])
    assert other.get_json()["participated"] is False
    not_observed = h.client.get(_public(h.tenant.slug, run) + "/respond/status?submission_id=vote-intent-0001", headers=h.headers["second"])
    assert not_observed.status_code == 404
    conflict = h.client.get(_public(h.tenant.slug, run) + "/respond/status?submission_id=other-intent-0001", headers=h.headers["actor"])
    assert conflict.status_code == 409
    assert _ledger_counts() == (1, 1)


def test_two_real_accounts_same_transport_ip_each_admitted_no_phone_cookie_identity(h):
    run = _create(h)["run_id"]
    assert _vote(h, run, environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 201
    assert _vote(h, run, "nat-second-0001", actor="second", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}).status_code == 201
    read = h.client.get(_public(h.tenant.slug, run) + "/results").get_json()
    assert read["metrics"]["total_responses"] == 2 and _ledger_counts() == (2, 2)
    assert all(row.actor_user_id is None and row.ip_address is None for row in AuditEvent.query.filter_by(event_type=service.ACCEPTED))


@pytest.mark.parametrize("actor", ["other", "orphan"])
def test_foreign_or_unresolved_membership_cannot_vote_or_read_private_own_receipt(h, actor):
    run = _create(h)["run_id"]
    assert _vote(h, run, actor=actor).status_code == 403
    status = h.client.get(_public(h.tenant.slug, run) + "/respond/status", headers=h.headers[actor])
    assert status.status_code == 403 and _ledger_counts() == (0, 0)


def test_conflicting_persisted_owner_and_direct_tenant_fail_closed(h):
    run = _create(h)["run_id"]
    h.actor.empresa_id = h.other.id
    db.session.commit()
    assert _vote(h, run).status_code == 403 and _ledger_counts() == (0, 0)


def test_no_bearer_forged_cookie_userid_and_retired_session_no_admission(h):
    run = _create(h)["run_id"]
    path = _public(h.tenant.slug, run) + "/respond"
    anonymous = h.client.post(path, json={"submission_id": "fake-id-0001", "option_id": "yes"}, headers={"Idempotency-Key": "fake-id-0001", "X-Anon-Id": "claimed"})
    assert anonymous.status_code == 401
    AuthSession.query.filter_by(actor_id=h.actor.id).update({"revoked_at": service._now()})
    db.session.commit()
    assert _vote(h, run).status_code == 401 and _ledger_counts() == (0, 0)


@pytest.mark.parametrize("header,value", [("X-Tenant-Slug", "rehearsal-b"), ("X-Tenant", "rehearsal-b"), ("X-Tenant-Id", "99999")])
def test_conflicting_selectors_fail_before_effects(h, header, value):
    run = _create(h)["run_id"]
    response = h.client.post(_public(h.tenant.slug, run) + "/respond", json={"submission_id": "scope-key-0001", "option_id": "yes"},
        headers={**h.headers["actor"], "Idempotency-Key": "scope-key-0001", header: value})
    assert response.status_code == 400 and _ledger_counts() == (0, 0)


@pytest.mark.parametrize("payload", [{"option_id": "yes"}, {"submission_id": "payload-key-0001", "option_id": {}},
    {"submission_id": "payload-key-0001", "option_id": "yes", "phone": "+15550000000"},
    {"submission_id": "mismatching-0001", "option_id": "yes"}])
def test_payload_and_intent_are_strict_without_self_asserted_identity(h, payload):
    run = _create(h)["run_id"]
    result = h.client.post(_public(h.tenant.slug, run) + "/respond", json=payload,
        headers={**h.headers["actor"], "Idempotency-Key": "payload-key-0001"})
    assert result.status_code == 400 and _ledger_counts() == (0, 0)


def test_bounded_body_limit_does_not_persist(h):
    run = _create(h)["run_id"]
    result = h.client.post(_public(h.tenant.slug, run) + "/respond", data=" " * 2049,
        headers={**h.headers["actor"], "Idempotency-Key": "large-key-0001"})
    assert result.status_code == 413 and _ledger_counts() == (0, 0)


def test_expiry_blocks_reads_replay_and_new_admission_without_reclassification(h, monkeypatch):
    data = _create(h)
    frozen = service._now() + timedelta(hours=25)
    monkeypatch.setattr(service, "_now", lambda: frozen)
    assert h.client.get(_public(h.tenant.slug, data["run_id"])).status_code == 410
    assert _vote(h, data["run_id"]).status_code == 410
    repeat = h.client.post(_admin(h.tenant.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": "create-intent-0001"})
    assert repeat.status_code == 410
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 1 and _ledger_counts() == (0, 0)


@pytest.mark.parametrize("invalid", ["instrument", "signature", "tenant", "audit_clock"])
def test_authority_receipt_cannot_be_forged_or_rebound(h, invalid):
    data = _create(h)
    row = AuditEvent.query.filter_by(event_type=service.CREATED).one()
    details = json.loads(json.dumps(row.details))
    if invalid == "instrument": details["authority"]["instrument_sha256"] = "0" * 64
    if invalid == "signature": details["authority_hmac"] = "0" * 64
    if invalid == "tenant": details["authority"]["tenant_id"] = h.foreign.id
    if invalid == "audit_clock": row.created_at = row.created_at - timedelta(hours=25)
    row.details = details; db.session.commit()
    assert h.client.get(_public(h.tenant.slug, data["run_id"])).status_code == 503
    assert _vote(h, data["run_id"]).status_code == 503 and _ledger_counts() == (0, 0)


def test_exact_canonical_tenant_run_and_license_gate(h):
    data = _create(h)
    assert h.client.get(_public(h.foreign.slug, data["run_id"])).status_code == 404
    h.tenant.is_active = False; db.session.commit()
    assert h.client.get(_public(h.tenant.slug, data["run_id"])).status_code == 403
    # Normal auth itself denies an inactive persisted organization first.
    assert _vote(h, data["run_id"]).status_code == 401 and _ledger_counts() == (0, 0)


def test_three_active_runs_and_expired_runs_do_not_reset_old_intention(h, monkeypatch):
    runs = [_create(h, f"create-quota-{index:04d}") for index in range(3)]
    fourth = h.client.post(_admin(h.tenant.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": "create-quota-0004"})
    assert fourth.status_code == 409 and AuditEvent.query.filter_by(event_type=service.CREATED).count() == 3
    descriptor = h.client.get(_admin(h.tenant.slug), headers=h.headers["sa"]).get_json()["create_action"]
    assert descriptor["can_create"] is False and descriptor["blocked_reason_code"] == "rehearsal_active_run_limit"


def test_twenty_distinct_real_accounts_quota_twentyfirst_rejected(h):
    run = _create(h)["run_id"]
    for index in range(21):
        user = User(name="Cuenta sintética", email=f"quota-{index}@rehearsal.test.invalid", rol="usuario",
                    tipo_chat="municipio", tenant_id=h.tenant.id, tenant_slug=h.tenant.slug)
        user.set_password("synthetic-offline-password")
        db.session.add(user); db.session.commit()
        headers = _headers(user)
        response = h.client.post(_public(h.tenant.slug, run) + "/respond", json={"submission_id": f"quota-vote-{index:04d}", "option_id": "yes"},
            headers={**headers, "Idempotency-Key": f"quota-vote-{index:04d}"})
        assert response.status_code == (201 if index < 20 else 409), response.get_json()
    assert _ledger_counts() == (20, 20)
    assert h.client.get(_public(h.tenant.slug, run) + "/results").get_json()["metrics"]["total_responses"] == 20


def test_one_account_thousand_new_client_keys_one_row_999_conflicts(h):
    run = _create(h)["run_id"]
    statuses = [_vote(h, run, f"rotation-{index:04d}").status_code for index in range(1000)]
    assert statuses.count(201) == 1 and statuses.count(409) == 999
    assert _ledger_counts() == (1, 1)
    assert h.client.get(_public(h.tenant.slug, run) + "/results").get_json()["metrics"]["total_responses"] == 1


@pytest.mark.parametrize("rate,expected", [({"available": False, "allowed": False}, 503), ({"available": True, "allowed": False}, 429)])
def test_rate_storage_failure_or_risk_limit_does_not_create_response(h, monkeypatch, rate, expected):
    from routes.v2 import survey_rehearsals as routes
    run = _create(h)["run_id"]
    monkeypatch.setattr(routes, "_consume_rate_limit", lambda *args, **kwargs: rate)
    assert _vote(h, run).status_code == expected and _ledger_counts() == (0, 0)


def test_missing_existing_ledger_fails503_rolls_back_creation_no_ddl(h):
    DemoSurveyParticipation.__table__.drop(db.engine)
    result = h.client.post(_admin(h.tenant.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": "missing-table-0001"})
    assert result.status_code == 503 and result.get_json()["reason_code"] == "rehearsal_storage_unavailable"
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 0


def test_failed_second_insert_rolls_back_vote_and_separate_audit_together(h):
    run = _create(h)["run_id"]
    def fail_audit(mapper, connection, target):
        if target.event_type == service.ACCEPTED:
            raise OperationalError("synthetic failure", {}, Exception("offline"))
    event.listen(AuditEvent, "before_insert", fail_audit)
    try:
        result = _vote(h, run)
    finally:
        event.remove(AuditEvent, "before_insert", fail_audit)
    assert result.status_code == 503 and _ledger_counts() == (0, 0)


def test_epoch_revalidation_loss_after_flush_rolls_back_both_effects(h):
    run = _create(h)["run_id"]
    h.lease.revalidate = lambda config: SimpleNamespace(allowed=False, enabled=True, epoch=7)
    response = _vote(h, run)
    assert response.status_code == 503 and _ledger_counts() == (0, 0)


@pytest.mark.parametrize("decision", [dict(allowed=False, enabled=True, epoch=6),
    dict(allowed=True, enabled=False, epoch=6), dict(allowed=True, enabled=True, epoch=7)])
def test_first_writer_authority_decision_denies_before_any_creation(h, decision):
    h.lease.decision = SimpleNamespace(**decision)
    response = h.client.post(_admin(h.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": "writer-denied-0001"})
    assert response.status_code == 503 and AuditEvent.query.count() == 0


def test_revocation_after_flush_before_commit_rolls_back_effects(h, monkeypatch):
    run = _create(h)["run_id"]
    original = service._commit
    def revoke_then_commit(lease, actor_id, tenant, **kwargs):
        AuthSession.query.filter_by(actor_id=actor_id).update({"revoked_at": service._now()})
        db.session.flush()
        return original(lease, actor_id, tenant, **kwargs)
    monkeypatch.setattr(service, "_commit", revoke_then_commit)
    assert _vote(h, run).status_code == 401 and _ledger_counts() == (0, 0)


def test_current_account_status_rejects_inconsistent_receipt_without_cached_pass(h):
    run = _create(h)["run_id"]
    assert _vote(h, run).status_code == 201
    row = DemoSurveyParticipation.query.one(); row.tenant_slug = h.foreign.slug; db.session.commit()
    assert h.client.get(_public(h.tenant.slug, run) + "/respond/status", headers=h.headers["actor"]).status_code == 503
    assert h.client.get(_public(h.tenant.slug, run) + "/results").status_code == 503


def _parallel(h, actions):
    barrier = threading.Barrier(len(actions))
    results = []
    def worker(action):
        with h.app.test_client() as client:
            barrier.wait(timeout=10)
            response = action(client)
            results.append((response.status_code, response.get_json()))
    threads = [threading.Thread(target=worker, args=(action,)) for action in actions]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads) and len(results) == len(actions)
    db.session.expire_all()
    return results


def test_concurrent_same_creation_key_has_one_signed_run(h):
    headers = {**h.headers["sa"], "Idempotency-Key": "concurrent-create-0001"}
    actions = [lambda client: client.post(_admin(h.slug), json={}, headers=headers) for _ in range(2)]
    results = _parallel(h, actions)
    assert sorted(status for status, _ in results) == [200, 201], results
    assert len({body["run_id"] for _, body in results}) == 1
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 1


def test_concurrent_different_keys_same_real_account_has_one_row(h):
    run = _create(h)["run_id"]
    actions = [lambda client, key=key: _vote(h, run, key, client=client, slug=h.slug) for key in ("race-account-0001", "race-account-0002")]
    results = _parallel(h, actions)
    assert sorted(status for status, _ in results) == [201, 409], results
    assert _ledger_counts() == (1, 1)


def test_concurrent_third_and_fourth_run_share_atomic_quota(h):
    _create(h, "initial-run-0001"); _create(h, "initial-run-0002")
    actions = [lambda client, key=key: client.post(_admin(h.slug), json={},
        headers={**h.headers["sa"], "Idempotency-Key": key}) for key in ("quota-race-0003", "quota-race-0004")]
    results = _parallel(h, actions)
    assert sorted(status for status, _ in results) == [201, 409], results
    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 3


@pytest.mark.skipif(os.getenv("CHATBOC_REHEARSAL_TEST_POSTGRES") != "1",
                    reason="Requires explicit disposable localhost PostgreSQL opt-in")
def test_disposable_postgres_advisory_locks_preserve_intent_account_and_both_quotas(monkeypatch):
    # Identical disposable server/role/database to the existing CI vault job.
    # No configurable provider DSN and no schema outside our fresh namespace.
    schema = "rehearsal_contract_" + uuid.uuid4().hex
    config = type("RehearsalPostgresConfig", (TestingConfig,), {
        "SQLALCHEMY_DATABASE_URI": "postgresql+psycopg2://postgres@127.0.0.1:5432/vaultcredregression",
        "SQLALCHEMY_ENGINE_OPTIONS": {"connect_args": {"options": f"-csearch_path={schema} -cstatement_timeout=10000 -clock_timeout=10000"}},
        "SECRET_KEY": "synthetic-rehearsal-test-secret-never-a-live-credential",
        "CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED": False, "CUTOVER_WRITER_FENCE_ENABLED": False,
        "ENABLE_RUNTIME_SCHEMA_SYNC": False, "ENABLE_RUNTIME_TENANT_INIT": False,
        "PUBLIC_ENCUESTAS_RATE_LIMIT": 10000, "ENFORCE_PUBLIC_SURVEY_DISTRIBUTED_RATE_LIMIT": False,
    })
    # TESTING create_app creates tables immediately; create its isolated
    # namespace first and keep cleanup outside the app factory's success path.
    bootstrap = create_engine(config.SQLALCHEMY_DATABASE_URI, **config.SQLALCHEMY_ENGINE_OPTIONS)
    created = False
    try:
        assert bootstrap.url.host == "127.0.0.1" and bootstrap.url.port == 5432
        assert bootstrap.url.database == "vaultcredregression" and bootstrap.url.username == "postgres"
        assert bootstrap.url.password is None and re.fullmatch(r"rehearsal_contract_[0-9a-f]{32}", schema)
        with bootstrap.begin() as setup:
            setup.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
            assert setup.execute(text("SELECT current_schema()")).scalar_one() == schema
        app = create_app(config)
        with app.app_context():
            assert db.engine.url == bootstrap.url
            try:
                with _local_harness(app, monkeypatch) as h:
                    actions = [lambda client: client.post(_admin(h.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": "pg-create-race-0001"}) for _ in range(2)]
                    results = _parallel(h, actions)
                    assert sorted(status for status, _ in results) == [200, 201], results
                    run = results[0][1]["run_id"]
                    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 1
                    actions = [lambda client, key=key: _vote(h, run, key, client=client, slug=h.slug) for key in ("pg-account-0001", "pg-account-0002")]
                    results = _parallel(h, actions)
                    assert sorted(status for status, _ in results) == [201, 409] and _ledger_counts() == (1, 1), results
                    for index in range(18):
                        voter = User(name="Cuenta sintética", email=f"pg-quota-{index}@rehearsal.test.invalid",
                            rol="usuario", tipo_chat="municipio", tenant_id=h.tenant.id, tenant_slug=h.slug,
                            password_hash=h.actor.password_hash)
                        db.session.add(voter); db.session.commit()
                        key = f"pg-quota-{index:04d}"
                        response = h.client.post(_public(h.slug, run) + "/respond", json={"submission_id": key, "option_id": "yes"},
                            headers={**_headers(voter), "Idempotency-Key": key})
                        assert response.status_code == 201, response.get_json()
                    actions = [lambda client, actor=actor: _vote(h, run, "pg-last-0001", client=client, slug=h.slug, actor=actor)
                               for actor in ("second", "owner")]
                    results = _parallel(h, actions)
                    assert sorted(status for status, _ in results) == [201, 409] and _ledger_counts() == (20, 20), results
                    _create(h, "pg-second-run-0001")
                    actions = [lambda client, key=key: client.post(_admin(h.slug), json={}, headers={**h.headers["sa"], "Idempotency-Key": key})
                               for key in ("pg-third-run-0001", "pg-fourth-run-0001")]
                    results = _parallel(h, actions)
                    assert sorted(status for status, _ in results) == [201, 409], results
                    assert AuditEvent.query.filter_by(event_type=service.CREATED).count() == 3
            finally:
                db.session.rollback(); db.session.remove()
                db.engine.dispose()
    finally:
        try:
            if created:
                assert re.fullmatch(r"rehearsal_contract_[0-9a-f]{32}", schema)
                with bootstrap.begin() as cleanup:
                    cleanup.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            bootstrap.dispose()


@pytest.mark.parametrize("case", ["preview", "render", "testing", "memory_db", "tls", "authority_off"])
def test_actual_production_boundary_fails_closed_without_probing_credentials(rehearsal_app, monkeypatch, case):
    # This test exercises the unpatched guard, with syntactic synthetic DSNs.
    monkeypatch.setenv("VERCEL", "1"); monkeypatch.setenv("VERCEL_ENV", "production")
    monkeypatch.delenv("RENDER", raising=False); monkeypatch.delenv("TESTING", raising=False)
    config = {"TESTING": False, "CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED": True,
              "CUTOVER_RUNTIME_IDENTITY": "vercel", "SQLALCHEMY_DATABASE_URI": "postgresql://synthetic.invalid.neon.tech/disposable?sslmode=verify-full"}
    if case == "preview": monkeypatch.setenv("VERCEL_ENV", "preview")
    if case == "render": monkeypatch.setenv("RENDER", "true")
    if case == "testing": config["TESTING"] = True
    if case == "memory_db": config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    if case == "tls": config["SQLALCHEMY_DATABASE_URI"] = "postgresql://synthetic.invalid.neon.tech/disposable?sslmode=require"
    if case == "authority_off": config["CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED"] = False
    with rehearsal_app.app_context():
        saved = {key: rehearsal_app.config[key] for key in config}
        rehearsal_app.config.update(config)
        try:
            with pytest.raises(service.RehearsalError) as error: service.require_production_runtime()
            assert error.value.status_code == 503
        finally:
            rehearsal_app.config.update(saved)
