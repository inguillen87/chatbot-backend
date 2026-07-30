from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import threading

from flask import Flask
import pytest
from sqlalchemy import select, update

from database import db
from models import DomainEffectOutbox
import services.domain_effect_outbox as runtime
from services.domain_effect_outbox import (
    DeliveredDomainEffect,
    DomainEffectConflictError,
    DomainEffectLeaseLostError,
    DomainEffectRegistry,
    DomainEffectValidationError,
    PermanentDomainEffectError,
    PreparedDomainEffect,
    SkippedDomainEffect,
    compose_domain_effect_registries,
    dispatch_domain_effects,
    recover_stale_domain_effects,
    stage_domain_effect,
    summarize_domain_effect_outbox,
)


BASE_TIME = datetime(2026, 7, 29, 15, 0, tzinfo=timezone.utc)
INTENT_SECRET = b"domain-effect-test-secret-32-bytes-minimum"
HANDLER_NAME = "ticket.email.v1"


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


@pytest.fixture()
def effect_app(tmp_path):
    app = _make_app(tmp_path / "domain-effect-runtime.sqlite3")
    with app.app_context():
        DomainEffectOutbox.__table__.create(db.engine)
        try:
            yield app
        finally:
            db.session.remove()
            DomainEffectOutbox.__table__.drop(db.engine)
            db.engine.dispose()


def _registry(prepare, *, payload_validator=None) -> DomainEffectRegistry:
    registry = DomainEffectRegistry()
    registry.register(
        HANDLER_NAME,
        prepare,
        payload_validator=payload_validator,
    )
    return registry


def _stage(
    registry: DomainEffectRegistry,
    *,
    tenant_id: int = 101,
    aggregate_ref: str = "40174",
    effect_key: str = "ticket:40174:email:requester",
    payload=None,
    max_attempts: int = 8,
    now: datetime = BASE_TIME,
    recipient_ref: str = "role:ticket.requester",
):
    return stage_domain_effect(
        tenant_id=tenant_id,
        aggregate_type="municipio_ticket",
        aggregate_ref=aggregate_ref,
        effect_type="ticket.created.requester_email.v1",
        handler_name=HANDLER_NAME,
        channel="email",
        recipient_ref=recipient_ref,
        effect_key=effect_key,
        intent_secret=INTENT_SECRET,
        registry=registry,
        payload=payload,
        max_attempts=max_attempts,
        available_at=now,
    )


def _success_registry(*, provider_ref: str | None = "provider-message-123"):
    return _registry(
        lambda _claim: PreparedDomainEffect(
            deliver=lambda: DeliveredDomainEffect(
                provider_ref=provider_ref,
                result={"ack_code": "accepted"},
            )
        )
    )


def _row(effect_id: int) -> DomainEffectOutbox:
    return db.session.get(DomainEffectOutbox, effect_id, populate_existing=True)


def test_registry_composition_is_a_deterministic_union():
    def prepare_alpha(_claim):
        return SkippedDomainEffect("alpha_disabled")

    def prepare_zeta(_claim):
        return SkippedDomainEffect("zeta_disabled")

    first = DomainEffectRegistry()
    first.register("zeta.handler.v1", prepare_zeta)
    second = DomainEffectRegistry()
    second.register("alpha.handler.v1", prepare_alpha)

    forward = compose_domain_effect_registries(first, second)
    reverse = compose_domain_effect_registries(second, first)

    assert forward.handler_names == ("alpha.handler.v1", "zeta.handler.v1")
    assert reverse.handler_names == forward.handler_names
    assert forward.resolve("alpha.handler.v1").prepare is prepare_alpha
    assert forward.resolve("zeta.handler.v1").prepare is prepare_zeta
    assert reverse.resolve("alpha.handler.v1").prepare is prepare_alpha
    assert reverse.resolve("zeta.handler.v1").prepare is prepare_zeta


def test_registry_composition_allows_the_same_registration_twice():
    def validate_payload(_payload):
        return None

    def prepare(_claim):
        return SkippedDomainEffect("handler_disabled")

    first = DomainEffectRegistry()
    first.register("shared.handler.v1", prepare, validate_payload)
    second = DomainEffectRegistry()
    second.register("shared.handler.v1", prepare, validate_payload)

    composed = compose_domain_effect_registries(first, second)

    assert composed.handler_names == ("shared.handler.v1",)
    assert composed.resolve("shared.handler.v1").prepare is prepare
    assert composed.resolve("shared.handler.v1").payload_validator is validate_payload


@pytest.mark.parametrize("conflict_kind", ("prepare", "validator"))
def test_registry_composition_rejects_duplicate_name_rebinding(conflict_kind):
    def prepare_one(_claim):
        return SkippedDomainEffect("one_disabled")

    def prepare_two(_claim):
        return SkippedDomainEffect("two_disabled")

    def validator_one(_payload):
        return None

    def validator_two(_payload):
        return None

    first = DomainEffectRegistry()
    first.register("shared.handler.v1", prepare_one, validator_one)
    second = DomainEffectRegistry()
    if conflict_kind == "prepare":
        second.register("shared.handler.v1", prepare_two, validator_one)
    else:
        second.register("shared.handler.v1", prepare_one, validator_two)

    with pytest.raises(
        DomainEffectConflictError,
        match="domain_effect_handler_already_registered",
    ):
        compose_domain_effect_registries(first, second)


def test_registry_composition_never_mutates_or_aliases_sources():
    def prepare_one(_claim):
        return SkippedDomainEffect("one_disabled")

    def prepare_two(_claim):
        return SkippedDomainEffect("two_disabled")

    first = DomainEffectRegistry()
    first.register("one.handler.v1", prepare_one)
    second = DomainEffectRegistry()
    second.register("two.handler.v1", prepare_two)
    first_names = first.handler_names
    second_names = second.handler_names

    composed = compose_domain_effect_registries(first, second)
    composed.register("three.handler.v1", prepare_one)

    assert composed is not first
    assert composed is not second
    assert first.handler_names == first_names == ("one.handler.v1",)
    assert second.handler_names == second_names == ("two.handler.v1",)
    assert composed.handler_names == (
        "one.handler.v1",
        "three.handler.v1",
        "two.handler.v1",
    )


def test_registry_composition_rejects_non_registry_sources():
    with pytest.raises(
        DomainEffectValidationError,
        match="domain_effect_registry_source_invalid",
    ):
        compose_domain_effect_registries(DomainEffectRegistry(), object())  # type: ignore[arg-type]


def test_stage_does_not_commit_the_callers_transaction(effect_app):
    registry = _success_registry()

    staged = _stage(registry)

    assert staged.replayed is False
    assert staged.effect_id > 0
    assert db.session().in_transaction()
    db.session.rollback()
    assert db.session.scalar(select(DomainEffectOutbox.id)) is None


def test_identical_replay_returns_same_row_and_changed_payload_fails_closed(effect_app):
    registry = _success_registry()
    first = _stage(registry, payload={"template_id": "ticket-created-v1"})
    db.session.commit()

    replay = _stage(registry, payload={"template_id": "ticket-created-v1"})

    assert replay.replayed is True
    assert replay.effect_id == first.effect_id
    with pytest.raises(DomainEffectConflictError):
        _stage(registry, payload={"template_id": "ticket-created-v2"})
    assert db.session.scalar(select(db.func.count(DomainEffectOutbox.id))) == 1


def test_concurrent_identical_stage_has_one_row_and_one_replay(effect_app):
    registry = _success_registry()
    barrier = threading.Barrier(2)

    def producer():
        with effect_app.app_context():
            barrier.wait(timeout=5)
            staged = _stage(registry)
            outcome = (staged.effect_id, staged.replayed)
            db.session.commit()
            db.session.remove()
            return outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: producer(), range(2)))

    with effect_app.app_context():
        assert db.session.scalar(select(db.func.count(DomainEffectOutbox.id))) == 1
    assert len({effect_id for effect_id, _replayed in outcomes}) == 1
    assert sorted(replayed for _effect_id, replayed in outcomes) == [False, True]


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "persona@example.test"},
        {"telefono": "5492615550101"},
        {"description": "Poste caido frente a la casa"},
        {"nested": {"body": "Reclamo privado"}},
        {"blob_ref": b"private-binary"},
    ],
)
def test_stage_rejects_direct_or_nested_pii_and_binary_payloads(effect_app, payload):
    registry = _success_registry()

    with pytest.raises(DomainEffectValidationError):
        _stage(registry, payload=payload)

    assert db.session.scalar(select(db.func.count(DomainEffectOutbox.id))) == 0


def test_stage_rejects_pii_alias_keys_even_when_value_looks_like_an_identifier(effect_app):
    registry = _success_registry()

    with pytest.raises(DomainEffectValidationError):
        _stage(registry, payload={"metadata": {"customer_phone": "5492615550101"}})

    assert db.session.scalar(select(db.func.count(DomainEffectOutbox.id))) == 0


def test_recipient_reference_cannot_disguise_a_raw_phone_as_a_role(effect_app):
    registry = _success_registry()

    with pytest.raises(DomainEffectValidationError):
        _stage(registry, recipient_ref="role:5492615550101")

    assert db.session.scalar(select(db.func.count(DomainEffectOutbox.id))) == 0


def test_success_marks_io_before_delivery_and_only_persists_provider_hash(effect_app):
    observed: dict[str, object] = {}

    def prepare(claim):
        def deliver():
            with db.session.no_autoflush:
                current = _row(claim.effect_id)
                observed["status"] = current.status
                observed["io_started_at"] = current.io_started_at
                observed["lease_token"] = current.lease_token
            return DeliveredDomainEffect(
                provider_ref="provider-private-id-456",
                result={"ack_code": "accepted"},
            )

        return PreparedDomainEffect(deliver=deliver)

    registry = _registry(prepare)
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.succeeded == 1
    assert observed["status"] == DomainEffectOutbox.STATUS_PROCESSING
    assert observed["io_started_at"] is not None
    assert observed["lease_token"]
    assert row.status == DomainEffectOutbox.STATUS_SUCCEEDED
    assert row.io_started_at is not None
    assert row.lease_token is None
    assert row.provider_ref_hash == hashlib.sha256(
        b"provider-private-id-456"
    ).hexdigest()
    assert "provider-private-id-456" not in str(row.result_json)


def test_explicit_preflight_skip_is_terminal_without_crossing_io_boundary(effect_app):
    registry = _registry(
        lambda _claim: SkippedDomainEffect(
            reason_code="recipient_not_configured",
            result={"policy_code": "no_destination"},
        )
    )
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.skipped == 1
    assert row.status == DomainEffectOutbox.STATUS_SKIPPED
    assert row.io_started_at is None
    assert row.processed_at is not None
    assert row.result_json == {
        "policy_code": "no_destination",
        "reason_code": "recipient_not_configured",
    }


def test_preflight_transient_failure_retries_then_dies_at_bound(effect_app):
    attempts = 0

    def prepare(_claim):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("private citizen narrative must only be digested")

    registry = _registry(prepare)
    staged = _stage(registry, max_attempts=2)
    db.session.commit()

    first = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )
    first_row = _row(staged.effect_id)
    assert first.retry_wait == 1
    assert first_row.status == DomainEffectOutbox.STATUS_RETRY_WAIT
    assert first_row.io_started_at is None
    assert first_row.attempt_count == 1
    assert first_row.last_error_code == "runtimeerror"
    assert len(first_row.last_error_digest) == 64
    assert "private citizen" not in str(first_row.last_error_digest)

    second = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME + timedelta(seconds=3),
    )
    final_row = _row(staged.effect_id)
    assert second.dead == 1
    assert final_row.status == DomainEffectOutbox.STATUS_DEAD
    assert final_row.processed_at is not None
    assert final_row.io_started_at is None
    assert final_row.attempt_count == 2
    assert attempts == 2


def test_failed_preflight_rolls_back_incidental_database_writes(effect_app):
    def prepare(claim):
        current = _row(claim.effect_id)
        current.result_json = {"marker_code": "must_not_commit"}
        raise RuntimeError("preflight failed")

    registry = _registry(prepare)
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.retry_wait == 1
    assert row.status == DomainEffectOutbox.STATUS_RETRY_WAIT
    assert row.result_json is None


def test_permanent_preflight_failure_dies_without_io_or_retry(effect_app):
    registry = _registry(
        lambda _claim: (_ for _ in ()).throw(
            PermanentDomainEffectError("destination_policy_invalid")
        )
    )
    staged = _stage(registry, max_attempts=8)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.dead == 1
    assert row.status == DomainEffectOutbox.STATUS_DEAD
    assert row.attempt_count == 1
    assert row.io_started_at is None
    assert row.last_error_code == "destination_policy_invalid"


def test_permanent_preflight_code_cannot_materialize_handler_supplied_pii(effect_app):
    registry = _registry(
        lambda _claim: (_ for _ in ()).throw(
            PermanentDomainEffectError("persona_private@example.test")
        )
    )
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.dead == 1
    assert row.status == DomainEffectOutbox.STATUS_DEAD
    assert "persona_private" not in str(row.last_error_code).lower()
    assert "example.test" not in str(row.last_error_code).lower()


@pytest.mark.parametrize(
    "deliver",
    [
        lambda: (_ for _ in ()).throw(RuntimeError("provider timed out with PII")),
        lambda: False,
        lambda: None,
        lambda: DeliveredDomainEffect(result={"message": "private_value"}),
        lambda: DeliveredDomainEffect(result={"customer_phone": "5492615550101"}),
    ],
    ids=("exception", "false", "none", "unsafe-ack", "unsafe-ack-alias"),
)
def test_every_unconfirmed_post_io_outcome_becomes_unknown_and_never_retries(
    effect_app,
    deliver,
):
    registry = _registry(
        lambda _claim: PreparedDomainEffect(deliver=deliver)  # type: ignore[arg-type]
    )
    staged = _stage(registry)
    db.session.commit()

    first = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )
    row = _row(staged.effect_id)
    assert first.unknown == 1
    assert row.status == DomainEffectOutbox.STATUS_UNKNOWN
    assert row.io_started_at is not None
    assert row.processed_at is not None
    assert row.lease_token is None
    assert len(row.last_error_digest) == 64
    assert "PII" not in str(row.last_error_digest)

    later = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME + timedelta(days=1),
    )
    assert later.processed == 0
    assert _row(staged.effect_id).attempt_count == 1


def test_operator_codes_never_materialize_pii_from_handler_strings(effect_app):
    registry = _registry(
        lambda _claim: SkippedDomainEffect(
            reason_code="persona_private@example.test",
        )
    )
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.skipped == 1
    rendered = str(row.result_json).lower()
    assert "persona_private" not in rendered
    assert "example.test" not in rendered


def test_expired_pre_io_lease_retries_with_backoff_and_then_can_be_reclaimed(effect_app):
    registry = _success_registry()
    staged = _stage(registry, max_attempts=2)
    db.session.commit()
    claim = runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME,
        session=db.session,
    )
    assert claim is not None

    recovered = recover_stale_domain_effects(
        now=BASE_TIME + timedelta(seconds=31)
    )

    waiting = _row(staged.effect_id)
    assert recovered == {"unknown": 0, "retry_wait": 1, "dead": 0}
    assert waiting.status == DomainEffectOutbox.STATUS_RETRY_WAIT
    assert waiting.io_started_at is None
    assert waiting.attempt_count == 1
    assert runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=32),
        session=db.session,
    ) is None

    replacement = runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME + timedelta(seconds=34),
        session=db.session,
    )
    assert replacement is not None
    assert replacement.lease_token != claim.lease_token
    assert replacement.attempt_count == 2


def test_expired_post_io_lease_is_unknown_and_never_reclaimed(effect_app):
    registry = _success_registry()
    staged = _stage(registry)
    db.session.commit()
    claim = runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME,
        session=db.session,
    )
    assert claim is not None
    runtime._fenced_transition(
        claim,
        values={"io_started_at": BASE_TIME},
        session=db.session,
    )

    recovered = recover_stale_domain_effects(
        now=BASE_TIME + timedelta(seconds=31)
    )

    row = _row(staged.effect_id)
    assert recovered == {"unknown": 1, "retry_wait": 0, "dead": 0}
    assert row.status == DomainEffectOutbox.STATUS_UNKNOWN
    assert row.io_started_at is not None
    assert row.processed_at is not None
    assert runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME + timedelta(days=1),
        session=db.session,
    ) is None


def test_expired_final_pre_io_lease_is_dead_instead_of_retried(effect_app):
    registry = _success_registry()
    staged = _stage(registry, max_attempts=1)
    db.session.commit()
    claim = runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME,
        session=db.session,
    )
    assert claim is not None

    recovered = recover_stale_domain_effects(
        now=BASE_TIME + timedelta(seconds=31)
    )

    row = _row(staged.effect_id)
    assert recovered == {"unknown": 0, "retry_wait": 0, "dead": 1}
    assert row.status == DomainEffectOutbox.STATUS_DEAD
    assert row.io_started_at is None
    assert row.processed_at is not None
    assert row.last_error_code == "lease_expired_attempts_exhausted"


def test_stale_lease_token_cannot_finalize_after_fence_moves(effect_app):
    registry = _success_registry()
    staged = _stage(registry)
    db.session.commit()
    claim = runtime._claim_next_domain_effect(
        tenant_id=None,
        lease_seconds=30,
        now=BASE_TIME,
        session=db.session,
    )
    assert claim is not None
    db.session.execute(
        update(DomainEffectOutbox)
        .where(DomainEffectOutbox.id == staged.effect_id)
        .values(
            lease_token="replacement-fence",
            leased_until=BASE_TIME + timedelta(minutes=5),
        )
    )
    db.session.commit()

    with pytest.raises(DomainEffectLeaseLostError):
        runtime._fenced_transition(
            claim,
            values={
                "status": DomainEffectOutbox.STATUS_SUCCEEDED,
                "lease_token": None,
                "leased_until": None,
                "processed_at": BASE_TIME,
            },
            session=db.session,
        )

    row = _row(staged.effect_id)
    assert row.status == DomainEffectOutbox.STATUS_PROCESSING
    assert row.lease_token == "replacement-fence"


def test_tampered_hmac_or_payload_is_dead_before_handler_runs(effect_app):
    prepared = False

    def prepare(_claim):
        nonlocal prepared
        prepared = True
        return PreparedDomainEffect(
            deliver=lambda: DeliveredDomainEffect(result={"ack_code": "accepted"})
        )

    registry = _registry(prepare)
    staged = _stage(registry, payload={"template_id": "ticket-created-v1"})
    db.session.commit()
    db.session.execute(
        update(DomainEffectOutbox)
        .where(DomainEffectOutbox.id == staged.effect_id)
        .values(payload_json={"template_id": "tampered-template"})
    )
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.dead == 1
    assert row.status == DomainEffectOutbox.STATUS_DEAD
    assert row.last_error_code == "intent_hmac_mismatch"
    assert row.io_started_at is None
    assert prepared is False


def test_wrong_worker_hmac_secret_fails_closed_before_prepare(effect_app):
    prepared = False

    def prepare(_claim):
        nonlocal prepared
        prepared = True
        return PreparedDomainEffect(
            deliver=lambda: DeliveredDomainEffect(result={"ack_code": "accepted"})
        )

    registry = _registry(prepare)
    staged = _stage(registry)
    db.session.commit()

    summary = dispatch_domain_effects(
        registry=registry,
        intent_secret=b"different-worker-secret-at-least-32-bytes",
        now=BASE_TIME,
    )

    row = _row(staged.effect_id)
    assert summary.dead == 1
    assert row.status == DomainEffectOutbox.STATUS_DEAD
    assert row.last_error_code == "intent_hmac_mismatch"
    assert row.io_started_at is None
    assert prepared is False


def test_stage_replay_rejects_an_existing_row_whose_immutable_fields_were_tampered(
    effect_app,
):
    registry = _success_registry()
    staged = _stage(registry)
    db.session.commit()
    db.session.execute(
        update(DomainEffectOutbox)
        .where(DomainEffectOutbox.id == staged.effect_id)
        .values(aggregate_ref="tampered-aggregate")
    )
    db.session.commit()

    with pytest.raises(DomainEffectConflictError):
        _stage(registry)


def test_atomic_claim_race_has_exactly_one_winner(effect_app):
    registry = _success_registry()
    _stage(registry)
    db.session.commit()
    barrier = threading.Barrier(2)

    def contender():
        with effect_app.app_context():
            barrier.wait(timeout=5)
            claim = runtime._claim_next_domain_effect(
                tenant_id=None,
                lease_seconds=30,
                now=BASE_TIME,
                session=db.session,
            )
            claim_token = claim.lease_token if claim else None
            db.session.remove()
            return claim_token

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(lambda _index: contender(), range(2)))

    winners = [token for token in tokens if token]
    assert len(winners) == 1
    assert len(set(winners)) == 1


def test_dispatch_and_metrics_are_tenant_scoped_and_pii_free(effect_app):
    registry = _success_registry()
    first = _stage(registry, tenant_id=101)
    second = _stage(
        registry,
        tenant_id=202,
        aggregate_ref="order-uuid-202",
        effect_key="order:uuid-202:email:requester",
    )
    db.session.commit()

    dispatched = dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        tenant_id=101,
        now=BASE_TIME,
    )
    tenant_one = summarize_domain_effect_outbox(
        tenant_id=101,
        now=BASE_TIME,
    )
    tenant_two = summarize_domain_effect_outbox(
        tenant_id=202,
        now=BASE_TIME,
    )
    global_summary = summarize_domain_effect_outbox(now=BASE_TIME)

    assert dispatched.succeeded == 1
    assert _row(first.effect_id).status == DomainEffectOutbox.STATUS_SUCCEEDED
    assert _row(second.effect_id).status == DomainEffectOutbox.STATUS_PENDING
    assert tenant_one["by_status"][DomainEffectOutbox.STATUS_SUCCEEDED] == 1
    assert tenant_one["by_status"][DomainEffectOutbox.STATUS_PENDING] == 0
    assert tenant_two["by_status"][DomainEffectOutbox.STATUS_PENDING] == 1
    assert tenant_two["due"] == 1
    assert global_summary["by_status"][DomainEffectOutbox.STATUS_SUCCEEDED] == 1
    assert global_summary["by_status"][DomainEffectOutbox.STATUS_PENDING] == 1
    rendered = str(global_summary)
    assert "role:ticket.requester" not in rendered
    assert "40174" not in rendered


def test_worker_must_not_begin_io_after_its_lease_has_expired(effect_app):
    delivered = False

    def prepare(claim):
        # Simulate a slow preflight whose lease expires before provider I/O.
        db.session.execute(
            update(DomainEffectOutbox)
            .where(DomainEffectOutbox.id == claim.effect_id)
            .values(leased_until=BASE_TIME - timedelta(seconds=1))
        )
        db.session.commit()

        def deliver():
            nonlocal delivered
            delivered = True
            return DeliveredDomainEffect(result={"ack_code": "accepted"})

        return PreparedDomainEffect(deliver=deliver)

    registry = _registry(prepare)
    staged = _stage(registry)
    db.session.commit()

    dispatch_domain_effects(
        registry=registry,
        intent_secret=INTENT_SECRET,
        now=BASE_TIME,
        lease_seconds=30,
    )

    row = _row(staged.effect_id)
    assert delivered is False
    assert row.io_started_at is None
    assert row.status in {
        DomainEffectOutbox.STATUS_RETRY_WAIT,
        DomainEffectOutbox.STATUS_DEAD,
    }
