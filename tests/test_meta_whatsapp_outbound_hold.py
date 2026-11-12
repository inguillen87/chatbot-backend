"""Independent outbound gate through native queue transitions and synthetic POST."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from models import db
from services import whatsapp_inbound_turns as queue, whatsapp_inbound_worker as worker
from services.meta_whatsapp_outbox import RUNTIME_EXTENSION, dispatch_meta_outbound_claim
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from tests.test_meta_whatsapp_outbox import _row, _stage, environment
from tests.test_whatsapp_inbound_turns import _complete_with_text, _ingest


FLAG = "META_WHATSAPP_OUTBOUND_ENABLED"


def _state(attempt_id):
    row = _row(attempt_id)
    return (row.status, row.attempt_count, row.available_at, row.last_error_code,
            row.last_error_digest, row.payload_digest, row.lease_token, row.leased_until)


def test_outbound_gate_defaults_off():
    assert Config.META_WHATSAPP_OUTBOUND_ENABLED is False


@pytest.mark.parametrize("flag", (None, False, "true", 1))
def test_disabled_worker_leaves_meta_pending_without_touching_graph(environment, flag):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    before = _state(attempt_id)
    if flag is None:
        env.app.config.pop(FLAG)
    else:
        env.app.config[FLAG] = flag
    reads = len(env.calls["get"])

    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)

    assert result.status == "idle" and _state(attempt_id) == before
    assert len(env.calls["get"]) == reads and env.calls["post"] == []
    env.app.config[FLAG] = True
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert _row(attempt_id).attempt_count == 1 and len(env.calls["post"]) == 1


def test_meta_hold_leaves_twilio_claim_available(environment):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    env.app.config[FLAG] = False
    now = datetime.now(timezone.utc)
    incoming = _ingest("SMSYNTHETICHOLDTWILIO", tenant_id=env.tenant.id, now=now)
    twilio = queue.claim_next_whatsapp_inbound_turn(tenant_id=env.tenant.id, now=now)
    assert twilio is not None and twilio.turn_id == incoming.turn_id
    assert _complete_with_text(twilio.turn_id, twilio.lease_token, now=now).completed

    outbound = queue.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)

    assert outbound is not None and outbound.provider == "twilio"
    assert _row(attempt_id).status == "pending" and _row(attempt_id).attempt_count == 0
    assert env.calls["post"] == []


@pytest.mark.parametrize("before_status", ("pending", "retry_wait"))
def test_disabled_after_claim_restores_prior_state_without_consuming_an_attempt(environment, before_status):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    if before_status == "retry_wait":
        first = queue.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
        assert first is not None
        assert queue.retry_whatsapp_outbound_attempt(first.attempt_id, first.lease_token,
            "synthetic_pre_post_failure", now=datetime.now(timezone.utc) - timedelta(minutes=2)) == "retry_wait"
    before = _state(attempt_id)
    assert before[0] == before_status
    claim = queue.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert claim is not None and claim.attempt_id == attempt_id
    reads = len(env.calls["get"])
    env.app.config[FLAG] = False
    started = []

    result = dispatch_meta_outbound_claim(claim, on_post_started=lambda: started.append(True))

    assert result.status == "held" and _state(attempt_id) == before
    assert len(env.calls["get"]) == reads and not started and env.calls["post"] == []


def test_retired_during_policy_check_is_rechecked_before_post(environment, monkeypatch):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    before = _state(attempt_id)
    reserve = WhatsAppEnterpriseRulesService.reserve_outbound
    policy_calls = []

    def retire_during_policy(*args, **kwargs):
        result = reserve(*args, **kwargs)
        policy_calls.append(result[0])
        env.app.config[FLAG] = False
        return result

    monkeypatch.setattr(WhatsAppEnterpriseRulesService, "reserve_outbound", retire_during_policy)

    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)

    assert result.status == "held" and policy_calls == [True]
    assert _state(attempt_id) == before and env.calls["post"] == []


def test_retired_after_post_starts_does_not_release_an_uncertain_attempt(environment):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    posts = []

    def lose_ack(method, _url, **_kwargs):
        posts.append(method)
        env.app.config[FLAG] = False
        raise TimeoutError("synthetic provider response lost")

    env.app.extensions[RUNTIME_EXTENSION] = replace(env.runtime, post=lose_ack)

    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)

    assert result.status == "send_uncertain" and posts == ["POST"]
    assert _row(attempt_id).status == "send_uncertain" and _row(attempt_id).attempt_count == 1
    env.app.config[FLAG] = True
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert posts == ["POST"]


def test_expired_outbound_claim_cannot_be_restored(environment):
    env = environment
    attempt_id = _stage(env).outbound_attempt_ids[0]
    claim = queue.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert claim is not None
    _row(attempt_id).leased_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.session.commit()
    env.app.config[FLAG] = False

    assert dispatch_meta_outbound_claim(claim, on_post_started=lambda: pytest.fail("POST started")).status == "lost_lease"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert _row(attempt_id).status == "send_uncertain" and env.calls["post"] == []
