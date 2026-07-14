from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import patch

from flask import Flask
import pytest

from database import db
from models import WebhookDelivery
from services import webhook_delivery_service
from services.webhook_delivery_service import (
    CLAIMED,
    DUPLICATE,
    IN_PROGRESS,
    claim_delivery,
    complete_delivery,
    fail_delivery,
    stage_delivery_completion,
)


@pytest.fixture()
def webhook_db(tmp_path):
    database_path = (tmp_path / "webhook-delivery.sqlite3").as_posix()
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False}},
    )
    db.init_app(app)

    with app.app_context():
        WebhookDelivery.__table__.create(db.engine)
        yield
        db.session.remove()
        WebhookDelivery.__table__.drop(db.engine)
        db.engine.dispose()


def test_claim_creates_normalized_digest_only_receipt(webhook_db):
    now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    body = b'{"user_id":"user_123","token":"must-not-be-stored"}'

    claim = claim_delivery(
        " Clerk ",
        " evt_123 ",
        "user.created",
        body,
        now=now,
    )

    assert claim.outcome == CLAIMED
    assert claim.should_process is True
    assert claim.attempts == 1
    assert claim.provider == "clerk"
    assert claim.event_id == "evt_123"

    receipt = db.session.get(WebhookDelivery, claim.delivery_id)
    assert receipt is not None
    assert receipt.payload_digest == hashlib.sha256(body).hexdigest()
    assert receipt.status == WebhookDelivery.STATUS_PROCESSING
    assert "payload" not in WebhookDelivery.__table__.columns
    assert b"must-not-be-stored".decode() not in str(receipt.__dict__)


def test_active_claim_is_in_progress_and_processed_claim_is_duplicate(webhook_db):
    started_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    first = claim_delivery("clerk", "evt_active", "session.created", b"{}", now=started_at)

    active = claim_delivery(
        "CLERK",
        "evt_active",
        "session.created",
        b"{}",
        now=started_at + timedelta(minutes=1),
    )
    assert active.outcome == IN_PROGRESS
    assert active.delivery_id == first.delivery_id
    assert active.attempts == 1

    assert complete_delivery(
        first.delivery_id,
        first.attempts,
        now=started_at + timedelta(minutes=2),
    )
    assert complete_delivery(first.delivery_id, first.attempts)

    duplicate = claim_delivery(
        "clerk",
        "evt_active",
        "session.created",
        b"{}",
        now=started_at + timedelta(minutes=3),
    )
    assert duplicate.outcome == DUPLICATE
    assert duplicate.attempts == 1
    assert WebhookDelivery.query.count() == 1
    assert db.session.get(WebhookDelivery, first.delivery_id).processed_at is not None


def test_staged_completion_shares_the_callers_transaction(webhook_db):
    claim = claim_delivery("clerk", "evt_atomic", "user.updated", b"{}")

    assert stage_delivery_completion(db.session, claim.delivery_id, claim.attempts)
    db.session.rollback()
    db.session.expire_all()
    assert db.session.get(WebhookDelivery, claim.delivery_id).status == WebhookDelivery.STATUS_PROCESSING

    assert stage_delivery_completion(db.session, claim.delivery_id, claim.attempts)
    db.session.commit()
    assert db.session.get(WebhookDelivery, claim.delivery_id).status == WebhookDelivery.STATUS_PROCESSED


def test_failed_delivery_can_be_retried_and_old_attempt_is_fenced(webhook_db):
    started_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    first = claim_delivery("clerk", "evt_retry", "user.updated", b"v1", now=started_at)

    assert fail_delivery(
        first.delivery_id,
        first.attempts,
        "Authorization: Bearer webhook-secret upstream_timeout",
        now=started_at + timedelta(seconds=10),
    )

    failed_receipt = db.session.get(WebhookDelivery, first.delivery_id)
    assert failed_receipt.status == WebhookDelivery.STATUS_FAILED
    assert failed_receipt.last_error == "Authorization=[REDACTED] upstream_timeout"
    assert "webhook-secret" not in failed_receipt.last_error
    db.session.remove()

    retry = claim_delivery(
        "clerk",
        "evt_retry",
        "user.updated",
        b"v2",
        now=started_at + timedelta(seconds=20),
    )
    assert retry.outcome == CLAIMED
    assert retry.is_retry is True
    assert retry.attempts == 2
    assert fail_delivery(first.delivery_id, first.attempts, "late_worker_error") is False

    receipt = db.session.get(WebhookDelivery, first.delivery_id)
    assert receipt.status == WebhookDelivery.STATUS_PROCESSING
    assert receipt.attempts == 2
    assert receipt.last_error is None
    assert receipt.payload_digest == hashlib.sha256(b"v2").hexdigest()


def test_stale_processing_delivery_is_reclaimed(webhook_db):
    started_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    first = claim_delivery("clerk", "evt_stale", "organization.updated", b"{}", now=started_at)

    reclaimed = claim_delivery(
        "clerk",
        "evt_stale",
        "organization.updated",
        b"{}",
        stale_after=timedelta(minutes=5),
        now=started_at + timedelta(minutes=6),
    )

    assert reclaimed.outcome == CLAIMED
    assert reclaimed.attempts == 2
    assert complete_delivery(first.delivery_id, first.attempts) is False
    assert complete_delivery(reclaimed.delivery_id, reclaimed.attempts) is True


def test_unique_insert_race_recovers_existing_active_delivery(webhook_db):
    started_at = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    first = claim_delivery("clerk", "evt_race", "user.created", b"{}", now=started_at)
    real_get_delivery = webhook_delivery_service._get_delivery
    calls = 0

    def stale_read_once(session, provider, event_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return real_get_delivery(session, provider, event_id)

    with patch.object(
        webhook_delivery_service,
        "_get_delivery",
        side_effect=stale_read_once,
    ):
        raced = claim_delivery(
            "clerk",
            "evt_race",
            "user.created",
            b"{}",
            now=started_at + timedelta(seconds=1),
        )

    assert raced.outcome == IN_PROGRESS
    assert raced.delivery_id == first.delivery_id
    assert raced.attempts == 1
    assert WebhookDelivery.query.count() == 1
