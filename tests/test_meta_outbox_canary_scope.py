"""Canary revocation is enforced on durable Meta sends, not just ingress."""
from copy import deepcopy
import json

import pytest

from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from services import meta_whatsapp_outbox as outbox
from services import whatsapp_inbound_turns as queue, whatsapp_inbound_worker as worker
from services.meta_whatsapp_runtime import MetaRuntimeError
from services.whatsapp_enterprise_rules import WhatsAppEnterpriseRulesService
from tests.test_meta_provider_isolation import _legacy_config, _isolate
from tests.test_meta_whatsapp_outbox import APP, WABA, PHONE, CONTACT, _stage, environment


KEY = "META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON"


@pytest.fixture
def isolated(environment):
    env = environment
    _legacy_config(env)
    _isolate(env)
    env.app.config["META_WHATSAPP_CALLBACK_ENABLED"] = True
    env.app.config[KEY] = json.dumps([{
        "app_id": APP, "waba_id": WABA, "phone_number_id": PHONE,
        "tenant_id": env.tenant.id, "contact": CONTACT,
    }])
    return env


def _change_scope(env, change):
    rows = json.loads(env.app.config[KEY])
    if change == "revoked":
        rows = []
    elif change == "contact":
        rows[0]["contact"] = "5491198765432"
    elif change == "tenant":
        rows[0]["tenant_id"] += 1
    elif change == "phone":
        rows[0]["phone_number_id"] = "400001"
    elif change == "waba":
        rows[0]["waba_id"] = "200002"
    elif change == "app":
        rows[0]["app_id"] = "100002"
    elif change == "expanded":
        rows.append({**rows[0], "contact": "5491198765432"})
    elif change == "isolation_off":
        env.app.config["META_WHATSAPP_PROVIDER_ISOLATION_ENABLED"] = False
    elif change == "callback_off":
        env.app.config["META_WHATSAPP_CALLBACK_ENABLED"] = False
    elif change == "callback_not_bool":
        env.app.config["META_WHATSAPP_CALLBACK_ENABLED"] = "true"
    elif change == "outbound_off":
        env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] = False
    else:
        raise AssertionError("Unknown synthetic scope change")
    env.app.config[KEY] = json.dumps(rows)


def _attempt(receipt):
    db.session.expire_all()
    return WhatsAppOutboundAttempt.query.filter_by(attempt_id=receipt.outbound_attempt_ids[0]).one()


def test_exact_canary_is_bound_to_envelope_and_sends_without_phone_transformation(isolated):
    env = isolated
    receipt = _stage(env)
    envelope = deepcopy(_attempt(receipt).payload_json)
    assert len(envelope["canary_scope_digest"]) == 64
    assert envelope["message"]["to"] == envelope["source"]["recipient"] == CONTACT
    assert env.calls["post"] == []
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "accepted" and len(env.calls["post"]) == 1
    assert env.calls["post"][0]["json"]["to"] == CONTACT and env.calls["twilio"] == 0


@pytest.mark.parametrize("change", (
    "revoked", "contact", "tenant", "phone", "waba", "app", "callback_off", "callback_not_bool",
))
def test_invalid_canary_cannot_stage_reply_or_inbox(isolated, change):
    env = isolated
    _change_scope(env, change)
    with pytest.raises(MetaRuntimeError, match="meta_outbox_canary_"):
        _stage(env)
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 0
    assert env.calls["post"] == []


@pytest.mark.parametrize("change", (
    "revoked", "contact", "tenant", "phone", "waba", "app", "expanded",
    "isolation_off", "callback_off", "callback_not_bool", "outbound_off",
))
def test_scope_changed_after_claim_never_posts(isolated, change):
    env = isolated
    receipt = _stage(env)
    claim = queue.claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id, providers=("meta",))
    assert claim is not None
    _change_scope(env, change)
    starts = []
    result = outbox.dispatch_meta_outbound_claim(claim, on_post_started=lambda: starts.append(True))
    assert result.status == ("held" if change == "outbound_off" else "dead"), result
    assert starts == [] and env.calls["post"] == [] and env.calls["twilio"] == 0
    assert _attempt(receipt).status == ("pending" if change == "outbound_off" else "dead")


def test_revocation_immediately_before_post_is_rechecked_and_never_retried(isolated, monkeypatch):
    env = isolated
    receipt = _stage(env)
    original_scope = env.app.config[KEY]
    reserve = WhatsAppEnterpriseRulesService.reserve_outbound
    policies = []
    def revoke_after_policy(*args, **kwargs):
        result = reserve(*args, **kwargs)
        policies.append(result[0])
        _change_scope(env, "revoked")
        return result
    monkeypatch.setattr(WhatsAppEnterpriseRulesService, "reserve_outbound", revoke_after_policy)
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert policies == [True] and result.status == "dead"
    assert env.calls["post"] == [] and _attempt(receipt).status == "dead"
    env.app.config[KEY] = original_scope
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
    assert env.calls["post"] == []


@pytest.mark.parametrize("change", ("recipient", "remove_proof", "invalid_proof"))
def test_altered_outbox_recipient_or_proof_cannot_send(isolated, change):
    env = isolated
    receipt = _stage(env)
    row = _attempt(receipt)
    payload = deepcopy(row.payload_json)
    if change == "recipient":
        payload["message"]["to"] = "5491198765432"
    elif change == "remove_proof":
        payload.pop("canary_scope_digest")
    else:
        payload["canary_scope_digest"] = "invalid"
    # Even coherent persisted payload+digest cannot bypass recipient authority.
    row.payload_json, row.payload_digest = payload, outbox._canonical(payload)
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead" and env.calls["post"] == []


def test_unrelated_sender_scope_change_does_not_invalidate_exact_sender(isolated):
    env = isolated
    _stage(env)
    rows = json.loads(env.app.config[KEY])
    rows.append({**rows[0], "phone_number_id": "400001", "tenant_id": env.tenant.id + 1})
    env.app.config[KEY] = json.dumps(rows)
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert len(env.calls["post"]) == 1


def test_outbound_hold_can_stage_but_cannot_claim_or_send(isolated):
    env = isolated
    env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] = False
    receipt = _stage(env)
    assert "canary_scope_digest" in _attempt(receipt).payload_json
    assert worker.dispatch_next_whatsapp_outbound_attempt().status == "idle"
    assert _attempt(receipt).status == "pending" and env.calls["post"] == []
