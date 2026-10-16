from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import Flask
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import db
from models import WhatsAppInboundTurn, WhatsAppOutboundAttempt
from services.whatsapp_inbound_turns import (
    INGEST_CREATED,
    INGEST_DUPLICATE,
    WhatsAppInboundDigestConflict,
    WhatsAppTurnValidationError,
    accept_whatsapp_outbound_attempt,
    claim_next_whatsapp_inbound_turn,
    claim_next_whatsapp_outbound_attempt,
    complete_whatsapp_inbound_turn,
    dead_whatsapp_inbound_turn,
    dead_whatsapp_outbound_attempt,
    ingest_whatsapp_inbound_turn,
    reconcile_whatsapp_outbound_status,
    renew_whatsapp_inbound_turn_lease,
    retry_whatsapp_outbound_attempt,
    retry_whatsapp_inbound_turn,
    scrub_expired_whatsapp_inbound_payloads,
    uncertain_whatsapp_outbound_attempt,
)


BASE_TIME = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)


def _make_app(database_path) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path.as_posix()}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            "connect_args": {"check_same_thread": False, "timeout": 5}
        },
    )
    db.init_app(app)
    return app


def _create_turn_tables() -> None:
    WhatsAppInboundTurn.__table__.create(db.engine)
    WhatsAppOutboundAttempt.__table__.create(db.engine)


def _drop_turn_tables() -> None:
    WhatsAppOutboundAttempt.__table__.drop(db.engine)
    WhatsAppInboundTurn.__table__.drop(db.engine)


@pytest.fixture()
def turn_app(tmp_path):
    app = _make_app(tmp_path / "whatsapp-durable-turns.sqlite3")
    with app.app_context():
        _create_turn_tables()
        try:
            yield app
        finally:
            db.session.remove()
            _drop_turn_tables()
            db.engine.dispose()


def _payload(sid: str, *, body: str = "Hay una luminaria caída") -> dict[str, str]:
    return {
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "From": "whatsapp:+5492615550101",
        "To": "whatsapp:+5492615550202",
        "Body": body,
    }


def _ingest(
    sid: str,
    *,
    tenant_id: int = 101,
    stream_key: str = "stream-a",
    body: str = "Hay una luminaria caída",
    now: datetime = BASE_TIME,
    payload_digest: str | None = None,
    max_attempts: int = 8,
    received_at: datetime | None = None,
):
    return ingest_whatsapp_inbound_turn(
        tenant_id=tenant_id,
        provider_message_sid=sid,
        stream_key=stream_key,
        payload=_payload(sid, body=body),
        payload_digest=payload_digest,
        max_attempts=max_attempts,
        received_at=received_at,
        now=now,
    )


def _complete_with_text(
    turn_id: str,
    lease_token: str,
    *,
    body: str = "Recibimos tu reclamo.",
    count: int = 1,
    now: datetime = BASE_TIME,
):
    outbound = [
        {
            "message_kind": "text",
            "payload": {"body": f"{body} {sequence}"},
        }
        for sequence in range(1, count + 1)
    ]
    return complete_whatsapp_inbound_turn(
        turn_id,
        lease_token,
        result={"intent": "crear_reclamo"},
        outbound=outbound,
        now=now,
    )


def _sensitive_payload(sid: str) -> dict[str, str]:
    return {
        "MessageSid": sid,
        "SmsMessageSid": sid,
        "From": "whatsapp:+5492615550101",
        "To": "whatsapp:+5492615550202",
        "ProfileName": "Persona Privada",
        "Body": "Poste caido frente a mi casa",
        "Latitude": "-34.6037",
        "Longitude": "-58.3816",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.example/private/photo.jpg",
        "MediaContentType0": "image/jpeg",
    }


def _assert_payload_is_scrubbed(payload: dict, digest: str) -> None:
    rendered = str(payload)
    for forbidden in (
        "Body",
        "From",
        "ProfileName",
        "Latitude",
        "Longitude",
        "MediaUrl0",
        "Persona Privada",
        "+5492615550101",
        "-34.6037",
        "private/photo.jpg",
    ):
        assert forbidden not in rendered
    marker = payload["_chatboc_payload_scrub"]
    assert marker["payload_digest_sha256"] == digest
    assert marker["retained_payload"] is False


def test_duplicate_sid_is_idempotent_but_changed_payload_is_a_conflict(turn_app):
    first = _ingest("SMduplicate001")
    replay = _ingest("SMduplicate001")

    assert first.outcome == INGEST_CREATED
    assert replay.outcome == INGEST_DUPLICATE
    assert replay.database_id == first.database_id
    assert replay.turn_id == first.turn_id
    assert replay.payload_digest == first.payload_digest

    with pytest.raises(WhatsAppInboundDigestConflict) as conflict:
        _ingest("SMduplicate001", body="El payload fue alterado")
    assert conflict.value.code == "inbound_payload_digest_conflict"

    with Session(db.engine) as session:
        assert session.query(WhatsAppInboundTurn).count() == 1


def test_completed_turn_atomically_scrubs_inbound_pii(turn_app):
    sid = "SMscrubcompleted001"
    receipt = ingest_whatsapp_inbound_turn(
        tenant_id=101,
        provider_message_sid=sid,
        stream_key="stream-sensitive-completed",
        payload=_sensitive_payload(sid),
        now=BASE_TIME,
    )
    claim = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert claim is not None
    completion = complete_whatsapp_inbound_turn(
        claim.turn_id,
        claim.lease_token,
        result={"intent": "crear_reclamo"},
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert completion.completed is True

    with Session(db.engine) as session:
        row = session.get(WhatsAppInboundTurn, receipt.database_id)
        assert row is not None
        _assert_payload_is_scrubbed(row.payload_json, receipt.payload_digest)


def test_completed_vcard_turn_keeps_only_scrub_tombstone(turn_app):
    sid = "SMscrubvcard001"
    private_filename = "persona-privada.vcf"
    private_url = "https://api.twilio.test/private/contact?token=secret"
    receipt = ingest_whatsapp_inbound_turn(
        tenant_id=101,
        provider_message_sid=sid,
        stream_key="stream-sensitive-vcard",
        payload={
            "MessageSid": sid,
            "SmsMessageSid": sid,
            "From": "whatsapp:+5492615550101",
            "To": "whatsapp:+5492615550202",
            "Body": private_filename,
            "NumMedia": "1",
            "MediaUrl0": private_url,
            "MediaContentType0": "text/vcard",
        },
        now=BASE_TIME,
    )
    claim = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert claim is not None
    assert claim.message_kind == "contact"
    completion = complete_whatsapp_inbound_turn(
        claim.turn_id,
        claim.lease_token,
        result={"outcome": "contact_acknowledged_without_import"},
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert completion.completed is True

    with Session(db.engine) as session:
        row = session.get(WhatsAppInboundTurn, receipt.database_id)
        assert row is not None
        _assert_payload_is_scrubbed(row.payload_json, receipt.payload_digest)
        rendered = str(row.payload_json)
        assert private_filename not in rendered
        assert private_url not in rendered
        assert "token=secret" not in rendered


def test_dead_payload_expires_after_retention_and_legal_hold_is_fail_closed(turn_app):
    sid = "SMscrubdead001"
    receipt = ingest_whatsapp_inbound_turn(
        tenant_id=101,
        provider_message_sid=sid,
        stream_key="stream-sensitive-dead",
        payload=_sensitive_payload(sid),
        now=BASE_TIME,
    )
    claim = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert claim is not None
    assert dead_whatsapp_inbound_turn(
        claim.turn_id,
        claim.lease_token,
        "permanent_policy_failure",
        now=BASE_TIME + timedelta(hours=1),
    ) == WhatsAppInboundTurn.STATUS_DEAD

    before_expiry = scrub_expired_whatsapp_inbound_payloads(
        dead_retention_hours=24,
        now=BASE_TIME + timedelta(hours=24, minutes=59),
    )
    assert before_expiry["scrubbed"] == 0

    held = scrub_expired_whatsapp_inbound_payloads(
        dead_retention_hours=24,
        legal_hold=True,
        now=BASE_TIME + timedelta(hours=25),
    )
    assert held["status"] == "legal_hold"
    assert held["eligible"] == 1
    assert held["scrubbed"] == 0

    expired = scrub_expired_whatsapp_inbound_payloads(
        dead_retention_hours=24,
        legal_hold=False,
        now=BASE_TIME + timedelta(hours=25),
    )
    assert expired["status"] == "completed"
    assert expired["dead_scrubbed"] == 1
    assert expired["remaining_eligible"] == 0
    assert "turn_id" not in expired
    assert "provider_message_sid" not in expired

    with Session(db.engine) as session:
        row = session.get(WhatsAppInboundTurn, receipt.database_id)
        assert row is not None
        _assert_payload_is_scrubbed(row.payload_json, receipt.payload_digest)


def test_caller_supplied_digest_must_match_the_canonical_payload(turn_app):
    first = _ingest("SMforgeddigest001")
    replay = _ingest(
        "SMforgeddigest001",
        payload_digest=first.payload_digest,
    )
    assert replay.outcome == INGEST_DUPLICATE

    with pytest.raises(WhatsAppTurnValidationError) as mismatch:
        _ingest(
            "SMforgeddigest001",
            body="Contenido diferente con el mismo digest declarado",
            payload_digest=first.payload_digest,
        )
    assert mismatch.value.code == "payload_digest_mismatch"


def test_fifo_blocks_later_inbound_turn_until_stream_head_completes(turn_app):
    first = _ingest("SMfifo001", stream_key="same-stream")
    second = _ingest(
        "SMfifo002",
        stream_key="same-stream",
        # Equal receipt times exercise the deterministic database-id tie break.
        now=BASE_TIME,
    )

    first_claim = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key="same-stream",
        now=BASE_TIME,
    )
    assert first_claim is not None
    assert first_claim.turn_id == first.turn_id
    assert claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key="same-stream",
        now=BASE_TIME,
    ) is None

    completion = complete_whatsapp_inbound_turn(
        first_claim.turn_id,
        first_claim.lease_token,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert completion.completed is True

    second_claim = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key="same-stream",
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert second_claim is not None
    assert second_claim.turn_id == second.turn_id


def test_fifo_claims_late_replay_by_original_received_at_and_keeps_it_stable(
    turn_app,
):
    stream_key = "late-replay-stream"
    newer = _ingest(
        "SMlateReplayNewer001",
        stream_key=stream_key,
        now=BASE_TIME + timedelta(seconds=10),
        received_at=BASE_TIME + timedelta(seconds=10),
    )
    older_replayed_late = _ingest(
        "SMlateReplayOlder001",
        stream_key=stream_key,
        now=BASE_TIME + timedelta(seconds=20),
        received_at=BASE_TIME,
    )

    first_claim = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key=stream_key,
        now=BASE_TIME + timedelta(seconds=20),
    )
    assert first_claim is not None
    assert first_claim.turn_id == older_replayed_late.turn_id

    completion = complete_whatsapp_inbound_turn(
        first_claim.turn_id,
        first_claim.lease_token,
        now=BASE_TIME + timedelta(seconds=21),
    )
    assert completion.completed is True

    second_claim = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key=stream_key,
        now=BASE_TIME + timedelta(seconds=21),
    )
    assert second_claim is not None
    assert second_claim.turn_id == newer.turn_id

    duplicate = _ingest(
        "SMlateReplayOlder001",
        stream_key=stream_key,
        now=BASE_TIME + timedelta(seconds=30),
        received_at=BASE_TIME - timedelta(minutes=5),
    )
    assert duplicate.outcome == INGEST_DUPLICATE

    with Session(db.engine) as session:
        persisted = session.get(
            WhatsAppInboundTurn,
            older_replayed_late.database_id,
        )
        assert persisted is not None
        assert persisted.received_at.replace(tzinfo=timezone.utc) == BASE_TIME
        assert persisted.created_at.replace(tzinfo=timezone.utc) == (
            BASE_TIME + timedelta(seconds=20)
        )


def test_expired_inbound_lease_reclaims_with_new_token_and_fences_stale_worker(turn_app):
    receipt = _ingest("SMleasefence001")
    original = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME,
    )
    assert original is not None

    replacement = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=31),
    )
    assert replacement is not None
    assert replacement.turn_id == receipt.turn_id
    assert replacement.lease_token != original.lease_token

    stale_completion = complete_whatsapp_inbound_turn(
        original.turn_id,
        original.lease_token,
        now=BASE_TIME + timedelta(seconds=32),
    )
    assert stale_completion.completed is False
    assert retry_whatsapp_inbound_turn(
        original.turn_id,
        original.lease_token,
        "stale_worker",
        now=BASE_TIME + timedelta(seconds=32),
    ) is None

    current_completion = complete_whatsapp_inbound_turn(
        replacement.turn_id,
        replacement.lease_token,
        now=BASE_TIME + timedelta(seconds=32),
    )
    assert current_completion.completed is True


def test_reclaiming_expired_inbound_lease_consumes_an_attempt(turn_app):
    _ingest("SMleaseattempt001", max_attempts=2)
    first = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME,
    )
    assert first is not None
    assert first.attempt_count == 1

    second = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=31),
    )
    assert second is not None
    assert second.attempt_count == 2


def test_outbound_attempts_are_fifo_within_a_stream(turn_app):
    _ingest("SMoutboundfifo001")
    inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert inbound is not None
    completion = _complete_with_text(
        inbound.turn_id,
        inbound.lease_token,
        count=2,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert completion.completed is True
    assert len(completion.outbound_attempt_ids) == 2

    first = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert first is not None
    assert first.sequence_no == 1
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=1),
    ) is None

    assert accept_whatsapp_outbound_attempt(
        first.attempt_id,
        first.lease_token,
        "SMprovideraccepted001",
        now=BASE_TIME + timedelta(seconds=2),
    ) is True
    second = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert second is not None
    assert second.sequence_no == 2


def test_pre_send_failure_retries_then_can_be_dead_lettered(turn_app):
    _ingest("SMpresendfailure001")
    inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert inbound is not None
    completion = _complete_with_text(
        inbound.turn_id,
        inbound.lease_token,
        now=BASE_TIME,
    )
    sending = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME,
    )
    assert sending is not None
    assert sending.attempt_id == completion.outbound_attempt_ids[0]

    assert retry_whatsapp_outbound_attempt(
        sending.attempt_id,
        sending.lease_token,
        "status_callback_unavailable",
        now=BASE_TIME + timedelta(seconds=1),
    ) == WhatsAppOutboundAttempt.STATUS_RETRY_WAIT
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=1),
    ) is None

    retrying = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(days=1),
    )
    assert retrying is not None
    assert dead_whatsapp_outbound_attempt(
        retrying.attempt_id,
        retrying.lease_token,
        "enterprise_policy_blocked",
        now=BASE_TIME + timedelta(days=1, seconds=1),
    ) == WhatsAppOutboundAttempt.STATUS_DEAD

    with Session(db.engine) as session:
        attempt = session.scalar(
            select(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id == retrying.attempt_id
            )
        )
        assert attempt is not None
        assert attempt.status == WhatsAppOutboundAttempt.STATUS_DEAD
        assert attempt.completed_at is not None
        assert attempt.provider_message_sid is None


def test_failed_send_cancels_same_turn_remainder_but_releases_future_turn(turn_app):
    _ingest("SMstreamfailure001", stream_key="stream-release")
    first_inbound = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key="stream-release",
        now=BASE_TIME,
    )
    assert first_inbound is not None
    first_completion = _complete_with_text(
        first_inbound.turn_id,
        first_inbound.lease_token,
        count=2,
        now=BASE_TIME,
    )

    _ingest(
        "SMstreamfailure002",
        stream_key="stream-release",
        now=BASE_TIME + timedelta(seconds=1),
    )
    second_inbound = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        stream_key="stream-release",
        now=BASE_TIME + timedelta(seconds=1),
    )
    assert second_inbound is not None
    second_completion = _complete_with_text(
        second_inbound.turn_id,
        second_inbound.lease_token,
        now=BASE_TIME + timedelta(seconds=1),
    )

    first_send = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=2),
    )
    assert first_send is not None
    assert first_send.attempt_id == first_completion.outbound_attempt_ids[0]
    assert accept_whatsapp_outbound_attempt(
        first_send.attempt_id,
        first_send.lease_token,
        "SMstreamfailureprovider001",
        now=BASE_TIME + timedelta(seconds=3),
    ) is True
    assert reconcile_whatsapp_outbound_status(
        tenant_id=101,
        attempt_id=first_send.attempt_id,
        provider_message_sid="SMstreamfailureprovider001",
        provider_status="failed",
        error="provider_rejected",
        now=BASE_TIME + timedelta(seconds=4),
    ) is True

    next_send = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=5),
    )
    assert next_send is not None
    assert next_send.attempt_id == second_completion.outbound_attempt_ids[0]

    with Session(db.engine) as session:
        cancelled = session.scalar(
            select(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id
                == first_completion.outbound_attempt_ids[1]
            )
        )
        assert cancelled is not None
        assert cancelled.status == WhatsAppOutboundAttempt.STATUS_CANCELLED
        assert cancelled.provider_status == "cancelled"


def test_expired_outbound_sending_becomes_uncertain_and_is_never_retried(turn_app):
    _ingest("SMuncertain001")
    inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert inbound is not None
    completion = _complete_with_text(
        inbound.turn_id,
        inbound.lease_token,
        now=BASE_TIME,
    )
    attempt_id = completion.outbound_attempt_ids[0]

    sending = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME,
    )
    assert sending is not None
    assert sending.attempt_id == attempt_id

    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=31),
    ) is None
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(days=1),
    ) is None
    assert accept_whatsapp_outbound_attempt(
        sending.attempt_id,
        sending.lease_token,
        "SMlateprovider001",
        now=BASE_TIME + timedelta(seconds=32),
    ) is False

    with Session(db.engine) as session:
        attempt = session.scalar(
            select(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id == attempt_id
            )
        )
        assert attempt is not None
        assert attempt.status == WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN
        assert attempt.attempt_count == 1
        assert attempt.lease_token is None
        assert attempt.completed_at is not None
        assert attempt.last_error_code == "send_lease_expired"


def test_same_provider_sid_and_stream_are_isolated_per_tenant(turn_app):
    tenant_one = _ingest(
        "SMtenantshared001",
        tenant_id=101,
        stream_key="shared-hash",
    )
    tenant_two = _ingest(
        "SMtenantshared001",
        tenant_id=202,
        stream_key="shared-hash",
    )

    assert tenant_one.outcome == INGEST_CREATED
    assert tenant_two.outcome == INGEST_CREATED
    assert tenant_one.database_id != tenant_two.database_id

    claim_one = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    claim_two = claim_next_whatsapp_inbound_turn(tenant_id=202, now=BASE_TIME)
    assert claim_one is not None and claim_one.tenant_id == 101
    assert claim_two is not None and claim_two.tenant_id == 202
    assert claim_one.lease_token != claim_two.lease_token

    with Session(db.engine) as session:
        rows = list(
            session.scalars(
                select(WhatsAppInboundTurn).order_by(WhatsAppInboundTurn.tenant_id)
            )
        )
        assert [(row.tenant_id, row.provider_message_sid) for row in rows] == [
            (101, "SMtenantshared001"),
            (202, "SMtenantshared001"),
        ]


def test_tenant_scoped_inbound_claim_does_not_dead_letter_other_tenant(turn_app):
    _ingest(
        "SMtenantlease202",
        tenant_id=202,
        stream_key="tenant-two-stream",
        max_attempts=1,
    )
    tenant_two = claim_next_whatsapp_inbound_turn(
        tenant_id=202,
        lease_seconds=30,
        now=BASE_TIME,
    )
    assert tenant_two is not None

    assert claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=31),
    ) is None

    with Session(db.engine) as session:
        row = session.scalar(
            select(WhatsAppInboundTurn).where(
                WhatsAppInboundTurn.tenant_id == 202
            )
        )
        assert row is not None
        assert row.status == WhatsAppInboundTurn.STATUS_PROCESSING
        assert row.lease_token == tenant_two.lease_token


def test_tenant_scoped_outbound_claim_does_not_mutate_other_tenant_lease(turn_app):
    for tenant_id in (101, 202):
        _ingest(
            f"SMtenantout{tenant_id}",
            tenant_id=tenant_id,
            stream_key="shared-outbound-stream",
        )
        inbound = claim_next_whatsapp_inbound_turn(
            tenant_id=tenant_id,
            now=BASE_TIME,
        )
        assert inbound is not None
        _complete_with_text(
            inbound.turn_id,
            inbound.lease_token,
            now=BASE_TIME,
        )
        sending = claim_next_whatsapp_outbound_attempt(
            tenant_id=tenant_id,
            lease_seconds=30,
            now=BASE_TIME,
        )
        assert sending is not None

    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=31),
    ) is None

    with Session(db.engine) as session:
        statuses = dict(
            session.execute(
                select(
                    WhatsAppOutboundAttempt.tenant_id,
                    WhatsAppOutboundAttempt.status,
                )
            ).all()
        )
    assert statuses[101] == WhatsAppOutboundAttempt.STATUS_SEND_UNCERTAIN
    assert statuses[202] == WhatsAppOutboundAttempt.STATUS_SENDING


def test_file_sqlite_turn_survives_app_restart_and_resumes_processing(tmp_path):
    database_path = tmp_path / "whatsapp-restart.sqlite3"
    app_one = _make_app(database_path)
    with app_one.app_context():
        _create_turn_tables()
        receipt = _ingest("SMrestart001")
        db.session.remove()
        db.engine.dispose()

    app_two = _make_app(database_path)
    with app_two.app_context():
        replay = _ingest("SMrestart001")
        assert replay.outcome == INGEST_DUPLICATE
        assert replay.turn_id == receipt.turn_id
        inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
        assert inbound is not None
        completion = _complete_with_text(
            inbound.turn_id,
            inbound.lease_token,
            now=BASE_TIME + timedelta(seconds=1),
        )
        assert completion.completed is True
        outbound_attempt_id = completion.outbound_attempt_ids[0]
        db.session.remove()
        db.engine.dispose()

    app_three = _make_app(database_path)
    with app_three.app_context():
        outbound = claim_next_whatsapp_outbound_attempt(
            tenant_id=101,
            now=BASE_TIME + timedelta(seconds=2),
        )
        assert outbound is not None
        assert outbound.attempt_id == outbound_attempt_id
        assert accept_whatsapp_outbound_attempt(
            outbound.attempt_id,
            outbound.lease_token,
            "SMrestartprovider001",
            now=BASE_TIME + timedelta(seconds=3),
        ) is True
        db.session.remove()
        _drop_turn_tables()
        db.engine.dispose()


def test_live_inbound_lease_can_be_renewed_but_stale_token_cannot(turn_app):
    _ingest("SMrenewlease001")
    claim = claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        lease_seconds=30,
        now=BASE_TIME,
    )
    assert claim is not None

    renewed_until = renew_whatsapp_inbound_turn_lease(
        claim.turn_id,
        claim.lease_token,
        lease_seconds=60,
        now=BASE_TIME + timedelta(seconds=20),
    )
    assert renewed_until == BASE_TIME + timedelta(seconds=80)
    assert claim_next_whatsapp_inbound_turn(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=31),
    ) is None
    assert renew_whatsapp_inbound_turn_lease(
        claim.turn_id,
        "stale-token",
        now=BASE_TIME + timedelta(seconds=21),
    ) is None


def test_delayed_outbound_is_not_claimable_before_available_at(turn_app):
    _ingest("SMdelayedoutbound001")
    inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert inbound is not None
    completion = complete_whatsapp_inbound_turn(
        inbound.turn_id,
        inbound.lease_token,
        outbound=[
            {
                "message_kind": "text",
                "payload": {"body": "Seguimiento durable"},
                "delay_seconds": 90,
            }
        ],
        now=BASE_TIME,
    )
    assert completion.completed is True
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=89),
    ) is None
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(seconds=90),
    ) is not None


def test_signed_callback_reconciles_uncertain_send_without_resend(turn_app):
    _ingest("SMcallbackreconcile001")
    inbound = claim_next_whatsapp_inbound_turn(tenant_id=101, now=BASE_TIME)
    assert inbound is not None
    completion = _complete_with_text(
        inbound.turn_id,
        inbound.lease_token,
        now=BASE_TIME,
    )
    outbound = claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME,
    )
    assert outbound is not None
    assert uncertain_whatsapp_outbound_attempt(
        outbound.attempt_id,
        outbound.lease_token,
        TimeoutError("provider timeout"),
        now=BASE_TIME + timedelta(seconds=1),
    ) is True

    assert reconcile_whatsapp_outbound_status(
        tenant_id=101,
        attempt_id=outbound.attempt_id,
        provider_message_sid="SMcallbackprovider001",
        provider_status="delivered",
        now=BASE_TIME + timedelta(seconds=2),
    ) is True
    assert reconcile_whatsapp_outbound_status(
        tenant_id=101,
        attempt_id=outbound.attempt_id,
        provider_message_sid="SMcallbackprovider001",
        provider_status="sent",
        now=BASE_TIME + timedelta(seconds=3),
    ) is True
    assert reconcile_whatsapp_outbound_status(
        tenant_id=101,
        attempt_id=outbound.attempt_id,
        provider_message_sid="SMcallbackprovider001",
        provider_status="failed",
        error="late_failure",
        now=BASE_TIME + timedelta(seconds=4),
    ) is True
    assert claim_next_whatsapp_outbound_attempt(
        tenant_id=101,
        now=BASE_TIME + timedelta(days=1),
    ) is None

    with Session(db.engine) as session:
        row = session.scalar(
            select(WhatsAppOutboundAttempt).where(
                WhatsAppOutboundAttempt.attempt_id
                == completion.outbound_attempt_ids[0]
            )
        )
        assert row is not None
        assert row.status == WhatsAppOutboundAttempt.STATUS_ACCEPTED
        assert row.provider_status == "delivered"
        assert row.provider_message_sid == "SMcallbackprovider001"
