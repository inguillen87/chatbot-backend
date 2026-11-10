"""Exercise the durable replay fence and native completion with synthetic identities."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from models import User, WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from services import whatsapp_inbound_turns as queue
from services.meta_whatsapp_outbox import MetaReplyContent, complete_meta_service_reply
from tests.test_meta_whatsapp_outbox import _pending_claim, environment
from tests.test_whatsapp_inbound_turns import BASE_TIME, _ingest, turn_app


def _arm(claim, now):
    return queue.arm_meta_whatsapp_inbound_execution(
        claim.turn_id, claim.lease_token, now=now,
    )


def _read_turn(claim):
    db.session.expire_all()
    return db.session.get(WhatsAppInboundTurn, claim.database_id)


def _now(environment):
    return datetime.fromtimestamp(environment.now, timezone.utc)


def test_armed_crash_is_reported_and_quarantined_without_new_lease(environment):
    claim = _pending_claim(environment)
    now = _now(environment)
    assert not queue.arm_meta_whatsapp_inbound_execution(claim.turn_id, "stale", now=now)
    assert _arm(claim, now)
    assert not _arm(claim, now)
    assert _read_turn(claim).last_error_code == "meta_execution_armed"
    assert queue.summarize_whatsapp_turn_health(claim.tenant_id, now=now)["inbound"]["commit_uncertain"] == 0

    expired = claim.leased_until + timedelta(seconds=1)
    health = queue.summarize_whatsapp_turn_health(claim.tenant_id, now=expired)["inbound"]
    assert health["due"] == 0 and health["commit_uncertain"] == 1
    assert queue.summarize_whatsapp_turn_health(claim.tenant_id + 1, now=expired)["inbound"]["commit_uncertain"] == 0
    assert queue.fail_meta_whatsapp_inbound_execution(
        claim.turn_id, claim.lease_token, "too_late", rollback_confirmed=True, now=expired,
    ) is None
    assert queue.claim_next_whatsapp_inbound_turn(tenant_id=claim.tenant_id, now=expired) is None
    row = _read_turn(claim)
    assert (row.status, row.last_error_code, row.attempt_count) == ("dead", "meta_commit_uncertain", 1)
    assert row.lease_token is None and row.leased_until is None
    assert queue.summarize_whatsapp_turn_health(claim.tenant_id, now=expired)["inbound"]["commit_uncertain"] == 1


def test_unarmed_meta_expiry_remains_reclaimable(environment):
    claim = _pending_claim(environment)
    expired = claim.leased_until + timedelta(seconds=1)
    assert not _arm(claim, expired)
    reclaimed = queue.claim_next_whatsapp_inbound_turn(tenant_id=claim.tenant_id, now=expired)
    assert reclaimed is not None and reclaimed.turn_id == claim.turn_id
    assert reclaimed.attempt_count == 2 and reclaimed.lease_token != claim.lease_token
    assert not _arm(claim, expired)
    assert _arm(reclaimed, expired)


@pytest.mark.parametrize("phase", ["arm_lock", "arm_commit", "release_lock"])
def test_clock_crossing_lease_boundary_cannot_authorize_work_or_retry(environment, monkeypatch, phase):
    claim = _pending_claim(environment)
    now = _now(environment)
    clock = [now]
    monkeypatch.setattr(queue, "_utc_now", lambda: clock[0])
    if phase == "release_lock":
        assert _arm(claim, now)

    def expire_clock(*_args):
        clock[0] = claim.leased_until

    def expire_after_read(_connection, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith("SELECT") and "whatsapp_inbound_turn" in statement:
            expire_clock()

    event_name, listener = (
        ("commit", expire_clock) if phase == "arm_commit"
        else ("after_cursor_execute", expire_after_read)
    )
    event.listen(db.engine, event_name, listener)
    try:
        if phase == "release_lock":
            assert queue.fail_meta_whatsapp_inbound_execution(
                claim.turn_id, claim.lease_token, "temporary_failure", rollback_confirmed=True,
            ) is None
        else:
            assert not queue.arm_meta_whatsapp_inbound_execution(claim.turn_id, claim.lease_token)
    finally:
        event.remove(db.engine, event_name, listener)
    row = _read_turn(claim)
    assert row.status == "processing"
    assert row.last_error_code == (None if phase == "arm_lock" else "meta_execution_armed")


@pytest.mark.parametrize("failure", ["explicit_uncertain", "generic_retry", "generic_dead"])
def test_failure_without_rollback_proof_cannot_erase_fence(environment, failure):
    claim = _pending_claim(environment)
    now = _now(environment)
    assert _arm(claim, now)
    # The legacy collector cannot complete a natively fenced Meta turn.
    assert not queue.complete_whatsapp_inbound_turn(claim.turn_id, claim.lease_token, now=now).completed
    operation = {
        "explicit_uncertain": queue.fail_meta_whatsapp_inbound_execution,
        "generic_retry": queue.retry_whatsapp_inbound_turn,
        "generic_dead": queue.dead_whatsapp_inbound_turn,
    }[failure]
    assert operation(claim.turn_id, claim.lease_token, "original_failure", now=now) == "dead"
    row = _read_turn(claim)
    assert row.last_error_code == "meta_commit_uncertain" and row.lease_token is None
    assert queue.claim_next_whatsapp_inbound_turn(tenant_id=claim.tenant_id, now=claim.leased_until) is None
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_confirmed_rollback_retries_under_a_fresh_lease(environment):
    claim = _pending_claim(environment)
    now = _now(environment)
    assert _arm(claim, now)
    assert queue.fail_meta_whatsapp_inbound_execution(
        claim.turn_id, claim.lease_token, "temporary_failure", rollback_confirmed=True, now=now,
    ) == "retry_wait"
    assert _read_turn(claim).last_error_code == "temporary_failure"
    assert queue.claim_next_whatsapp_inbound_turn(tenant_id=claim.tenant_id, now=now) is None
    next_time = now + timedelta(seconds=queue.BASE_BACKOFF_SECONDS)
    reclaimed = queue.claim_next_whatsapp_inbound_turn(tenant_id=claim.tenant_id, now=next_time)
    assert reclaimed is not None and reclaimed.turn_id == claim.turn_id
    assert reclaimed.lease_token != claim.lease_token and _arm(reclaimed, next_time)
    assert queue.fail_meta_whatsapp_inbound_execution(
        reclaimed.turn_id, reclaimed.lease_token, "unsupported_turn",
        rollback_confirmed=True, permanent=True, now=next_time,
    ) == "dead"
    assert _read_turn(claim).last_error_code == "unsupported_turn"
    assert queue.summarize_whatsapp_turn_health(claim.tenant_id, now=next_time)["inbound"]["commit_uncertain"] == 0


@pytest.mark.parametrize("commit", [False, True])
def test_native_completion_clears_fence_only_in_committed_business_transaction(environment, commit):
    claim = _pending_claim(environment)
    now = _now(environment)
    owner_id = environment.owner.id
    previous_name = environment.owner.name
    assert _arm(claim, now)
    with Session(bind=db.engine) as session:
        owner = session.get(User, owner_id)
        owner.name = "Synthetic business change"
        session.flush()
        receipt = complete_meta_service_reply(
            claim, MetaReplyContent("text", "Synthetic reply"),
            runtime=environment.runtime, session=session,
        )
        assert not receipt.committed and len(receipt.outbound_attempt_ids) == 1
        if commit:
            session.commit()
        else:
            session.rollback()
    row = _read_turn(claim)
    if commit:
        assert (row.status, row.last_error_code) == ("completed", None)
        assert db.session.get(User, owner_id).name == "Synthetic business change"
        assert WhatsAppOutboundAttempt.query.count() == 1
    else:
        assert (row.status, row.last_error_code) == ("processing", "meta_execution_armed")
        assert db.session.get(User, owner_id).name == previous_name
        assert WhatsAppOutboundAttempt.query.count() == 0
    assert environment.calls["post"] == [] and environment.calls["twilio"] == 0


def test_twilio_cannot_arm_and_keeps_existing_expiry_replay(turn_app):
    receipt = _ingest("SMFENCETWILIOSYNTHETIC")
    claim = queue.claim_next_whatsapp_inbound_turn(now=BASE_TIME)
    assert claim is not None and claim.turn_id == receipt.turn_id
    assert not _arm(claim, BASE_TIME)
    reclaimed = queue.claim_next_whatsapp_inbound_turn(now=claim.leased_until)
    assert reclaimed is not None and reclaimed.turn_id == claim.turn_id
    assert reclaimed.lease_token != claim.lease_token and reclaimed.attempt_count == 2
    assert queue.complete_whatsapp_inbound_turn(
        reclaimed.turn_id, reclaimed.lease_token, now=claim.leased_until,
    ).completed
