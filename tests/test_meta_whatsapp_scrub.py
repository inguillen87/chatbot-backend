"""One new real producer→privacy scrub→worker regression; no live HTTP."""
from datetime import datetime, timezone
import copy
import json

from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt, db
from services import whatsapp_inbound_worker as worker
from services.meta_whatsapp_outbox import _canonical
from services.whatsapp_inbound_turns import scrub_expired_whatsapp_inbound_payloads
from tests.test_meta_whatsapp_outbox import environment, _stage


from dataclasses import replace
from datetime import timedelta
import secrets

import pytest
from services import meta_whatsapp_outbox as meta_outbox
from services import meta_whatsapp_credentials as vault
from services.meta_whatsapp_outbox import MetaReplyContent, complete_meta_service_reply
from services.whatsapp_inbound_turns import claim_next_whatsapp_outbound_attempt
from tests.test_meta_whatsapp_outbox import _pending_claim, ACK


def test_completed_meta_source_survives_privacy_scrub_before_dispatch(environment):
    env = environment
    binding = env.runtime.load_binding(env.event.sender)
    now = env.runtime.now()
    assert binding.authority.observed_at <= now < binding.authority.valid_until
    assert now < binding.credential.expires_at
    assert 0 <= now - env.event.timestamp < 86400
    receipt = _stage(env)
    assert len(receipt.outbound_attempt_ids) == 1
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=receipt.turn_id).one()
    attempt = WhatsAppOutboundAttempt.query.filter_by(attempt_id=receipt.outbound_attempt_ids[0]).one()
    assert turn.status == "completed" and attempt.status == "pending"
    reference_digest = _canonical(turn.payload_json)
    assert turn.result_json["meta_source_digest"] == reference_digest
    original_outbox = copy.deepcopy(attempt.payload_json)
    original_outbox_digest = attempt.payload_digest
    report = scrub_expired_whatsapp_inbound_payloads(
        tenant_id=env.tenant.id, limit=5, dead_retention_hours=72, legal_hold=False,
        now=datetime.fromtimestamp(now, timezone.utc))
    assert report["completed_scrubbed"] == 1 and report["dead_scrubbed"] == 0
    db.session.expire_all()
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=receipt.turn_id).one()
    marker = turn.payload_json["_chatboc_payload_scrub"]
    assert set(turn.payload_json) == {"_chatboc_payload_scrub"}
    assert marker["reason"] == "completed" and marker["retained_payload"] is False
    assert turn.result_json["meta_source_digest"] == reference_digest
    attempt = WhatsAppOutboundAttempt.query.filter_by(attempt_id=receipt.outbound_attempt_ids[0]).one()
    assert attempt.status == "pending" and attempt.payload_json == original_outbox
    assert attempt.payload_digest == original_outbox_digest
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
    # Immediate completed scrub: neither the 24h service window, the 60s
    # authority TTL nor the credential has expired in this synthetic clock.
    assert env.runtime.now() == now
    assert now < binding.authority.valid_until and now < binding.credential.expires_at
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    print(json.dumps({"workerStatus": result.status, "errorCode": result.error_code,
        "metaPostCount": len(env.calls["post"]), "twilioCount": env.calls["twilio"],
        "serviceAgeSeconds": now - env.event.timestamp,
        "remainingAuthoritySeconds": binding.authority.valid_until - now,
        "remainingCredentialSeconds": binding.credential.expires_at - now}))
    assert result.status == "accepted", "Meta completed source must remain dispatchable after privacy scrub"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0



def _scrub(env, receipt, *, microseconds=0):
    report = scrub_expired_whatsapp_inbound_payloads(tenant_id=env.tenant.id, limit=5,
        legal_hold=False, now=datetime.fromtimestamp(env.now, timezone.utc) + timedelta(microseconds=microseconds))
    assert report["completed_scrubbed"] == 1
    db.session.expire_all()
    return WhatsAppInboundTurn.query.filter_by(turn_id=receipt.turn_id).one()


def _outbox(receipt):
    db.session.expire_all()
    return WhatsAppOutboundAttempt.query.filter_by(attempt_id=receipt.outbound_attempt_ids[0]).one()


def test_scrub_duplicate_preserves_same_turn_outbox_and_privacy(environment):
    env = environment
    first = _stage(env)
    _scrub(env, first)
    again = _stage(env)
    assert again.outcome == "duplicate" and again.turn_id == first.turn_id
    assert again.outbound_attempt_ids == first.outbound_attempt_ids
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    last = _stage(env)
    assert last.outbound_attempt_ids == first.outbound_attempt_ids
    db.session.expire_all()
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=first.turn_id).one()
    assert set(turn.payload_json) == {"_chatboc_payload_scrub"}
    assert turn.payload_json["_chatboc_payload_scrub"]["retained_payload"] is False
    assert _outbox(first).provider_message_sid == ACK
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_scrub_claimed_readable_input_links_original_digest_before_dispatch(environment):
    env = environment
    claim = _pending_claim(env)
    receipt = complete_meta_service_reply(claim, MetaReplyContent("text", "Calculated reply"), runtime=env.runtime)
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=claim.turn_id).one()
    assert turn.payload_digest == claim.payload_digest
    assert turn.result_json["meta_inbound_payload_digest"] == claim.payload_digest
    assert turn.payload_digest != _canonical(turn.payload_json)
    _scrub(env, receipt)
    duplicate = _stage(env)
    assert duplicate.outcome == "duplicate" and duplicate.turn_id == claim.turn_id
    assert duplicate.outbound_attempt_ids == receipt.outbound_attempt_ids
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert WhatsAppInboundTurn.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


@pytest.mark.parametrize("change", ["marker_missing", "marker_schema", "missing_field", "contract", "reason", "retained", "marker_digest",
    "native_digest", "source_digest", "extra_top", "extra_marker", "kind", "fields", "bytes",
    "bad_time", "future_time", "before_completion", "original_link"])
def test_scrub_marker_tampering_stays_denied_before_post(environment, change):
    env = environment
    receipt = _stage(env)
    turn = _scrub(env, receipt)
    payload = copy.deepcopy(turn.payload_json)
    marker = payload["_chatboc_payload_scrub"]
    if change == "marker_missing": payload = {}
    elif change == "marker_schema": payload["_chatboc_payload_scrub"] = []
    elif change == "missing_field": marker.pop("scrubbed_at")
    elif change == "contract": marker["contract_version"] = "unknown.scrub.v0"
    elif change == "reason": marker["reason"] = "dead_retention_expired"
    elif change == "retained": marker["retained_payload"] = True
    elif change == "marker_digest": marker["payload_digest_sha256"] = "0" * 64
    elif change == "native_digest": turn.payload_digest = "0" * 64
    elif change == "source_digest": turn.result_json = {**turn.result_json, "meta_source_digest": "0" * 64}
    elif change == "extra_top": payload["extra"] = "synthetic-extra-not-authority"
    elif change == "extra_marker": marker["extra"] = "synthetic-extra-not-authority"
    elif change == "kind": marker["message_kind"] = "audio"
    elif change == "fields": marker["original_fields"] = 1
    elif change == "bytes": marker["original_bytes"] = 1
    elif change == "bad_time": marker["scrubbed_at"] = "not-a-date"
    elif change == "future_time": marker["scrubbed_at"] = datetime.fromtimestamp(env.now + 1, timezone.utc).isoformat()
    elif change == "before_completion": marker["scrubbed_at"] = datetime.fromtimestamp(env.now - 1, timezone.utc).isoformat()
    else: turn.result_json = {**turn.result_json, "meta_inbound_payload_digest": None}
    turn.payload_json = payload
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead"
    assert _outbox(receipt).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("change", ["tenant", "connection", "sender", "app", "waba", "phone",
                                    "recipient", "wamid", "native_wamid"])
def test_scrub_does_not_relax_native_source_scope(environment, change):
    env = environment
    receipt = _stage(env)
    turn = _scrub(env, receipt)
    row = _outbox(receipt)
    payload = copy.deepcopy(row.payload_json)
    identity = payload["identity"]
    keys = {"tenant": "tenant_id", "connection": "connection_id", "sender": "sender_id",
            "app": "app_id", "waba": "waba_id", "phone": "phone_number_id"}
    if change in keys:
        key = keys[change]
        identity[key] = identity[key] + 1000 if type(identity[key]) is int else "999999"
    elif change == "recipient":
        payload["source"]["recipient"] = payload["message"]["to"] = "5499999999999"
    elif change == "wamid": payload["source"]["message_id"] = "wamid.foreign/SAFE+="
    else: turn.provider_message_sid = "wamid.foreign/SAFE+="
    row.payload_json = payload
    row.payload_digest = _canonical(payload)
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead" and _outbox(receipt).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("case", ["stale_owner", "expired"])
def test_scrub_preserves_the_existing_lease_fences(environment, case):
    env = environment
    receipt = _stage(env)
    _scrub(env, receipt)
    claim = claim_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert claim is not None
    if case == "stale_owner":
        result = meta_outbox.dispatch_meta_outbound_claim(replace(claim, lease_token="stale-owner"),
            on_post_started=lambda: None, runtime=env.runtime)
        assert result.status == "lost_lease" and _outbox(receipt).status == "sending"
        assert env.calls["post"] == [] and env.calls["twilio"] == 0
        valid = meta_outbox.dispatch_meta_outbound_claim(claim, on_post_started=lambda: None, runtime=env.runtime)
        assert valid.status == "accepted" and len(env.calls["post"]) == 1
    else:
        row = _outbox(receipt)
        row.leased_until = datetime.fromtimestamp(env.now - 1, timezone.utc)
        db.session.commit()
        assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "idle"
        assert _outbox(receipt).status == "send_uncertain"
        assert env.calls["post"] == [] and env.calls["twilio"] == 0


@pytest.mark.parametrize("case", ["credential_revision", "sender_revoked"])
def test_scrub_does_not_override_retired_provider_authority(environment, case):
    env = environment
    receipt = _stage(env)
    _scrub(env, receipt)
    if case == "credential_revision":
        env.connection.config = {vault.PRIVATE_CONFIG_KEY: vault.seal_token(
            connection=env.connection, tenant_id=env.tenant.id, access_token=secrets.token_urlsafe(64),
            revision=2, expires_at=env.now + 3600, now=env.now, app_config=env.app.config)}
    else: env.sender.status = "OFFLINE"
    db.session.commit()
    result = worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id)
    assert result.status == "dead" and _outbox(receipt).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_scrub_legacy_convenience_digest_is_exact_without_new_metadata(environment):
    env = environment
    receipt = _stage(env)
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=receipt.turn_id).one()
    assert turn.payload_digest == turn.result_json["meta_source_digest"]
    result = dict(turn.result_json)
    result.pop("meta_inbound_payload_digest")
    turn.result_json = result
    db.session.commit()
    _scrub(env, receipt)
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0


def test_scrub_unknown_original_digest_link_does_not_gain_authority(environment):
    env = environment
    claim = _pending_claim(env)
    receipt = complete_meta_service_reply(claim, MetaReplyContent("text", "Calculated reply"), runtime=env.runtime)
    turn = WhatsAppInboundTurn.query.filter_by(turn_id=claim.turn_id).one()
    assert turn.payload_digest != turn.result_json["meta_source_digest"]
    result = dict(turn.result_json)
    result.pop("meta_inbound_payload_digest")
    turn.result_json = result
    db.session.commit()
    _scrub(env, receipt)
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "dead"
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_real_scrub_microseconds_in_current_observed_second_remain_valid(environment):
    env = environment
    receipt = _stage(env)
    _scrub(env, receipt, microseconds=500000)
    assert env.runtime.now() == env.now
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=env.tenant.id).status == "accepted"
    assert len(env.calls["post"]) == 1 and env.calls["twilio"] == 0
