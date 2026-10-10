from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from services.domain_effect_gate import DomainEffectOutboxConfigurationError
from services.domain_effect_outbox import DomainEffectDispatchSummary
from services.order_domain_effects import ORDER_DOMAIN_EFFECT_REGISTRY
from services.ticket_domain_effects import TICKET_DOMAIN_EFFECT_REGISTRY
from services import domain_effect_worker as worker


SECRET = "worker-test-secret-" + ("x" * 32)


def test_worker_registry_composes_every_persisted_domain_handler():
    expected = set(TICKET_DOMAIN_EFFECT_REGISTRY.handler_names)
    expected.update(ORDER_DOMAIN_EFFECT_REGISTRY.handler_names)

    assert set(worker.DOMAIN_EFFECT_WORKER_REGISTRY.handler_names) == expected


def _legacy(app, monkeypatch) -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "legacy")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", "")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", "")


def _queue(app, monkeypatch, tenant_ids: str = "101") -> None:
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_MODE", "queue")
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_SECRET", SECRET)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_TENANT_IDS", tenant_ids)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_WORKER_BATCH_SIZE", 20)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_LEASE_SECONDS", 180)


def test_legacy_worker_idles_without_error(app, monkeypatch):
    _legacy(app, monkeypatch)

    with app.app_context():
        report = worker.dispatch_domain_effect_batch()
    run = worker.run_domain_effect_worker(app, once=True)

    assert report["processed"] == 0
    assert report["tenants"] == []
    assert run == {
        "contract_version": "domain.effect_worker_run.v1",
        "cycles": 1,
        "processed": 0,
        "succeeded": 0,
        "skipped": 0,
        "retry_wait": 0,
        "unknown": 0,
        "dead": 0,
        "recovered_unknown": 0,
        "recovered_retry_wait": 0,
        "recovered_dead": 0,
        "cycle_failures": 0,
    }


def test_explicit_dispatch_requires_queue_canary(app, monkeypatch):
    _legacy(app, monkeypatch)

    with app.app_context():
        with pytest.raises(
            DomainEffectOutboxConfigurationError,
            match="domain_effect_worker_requires_queue_mode",
        ):
            worker.dispatch_domain_effect_batch(tenant_id=101)


def test_queue_worker_dispatches_only_explicit_canaries(app, monkeypatch):
    _queue(app, monkeypatch, "202,101")
    summary = DomainEffectDispatchSummary(
        processed=1,
        succeeded=1,
        skipped=0,
        unknown=0,
        retry_wait=0,
        dead=0,
    )

    with app.app_context(), patch.object(
        worker,
        "dispatch_domain_effects",
        return_value=summary,
    ) as dispatch:
        report = worker.dispatch_domain_effect_batch(limit=2)

    assert [call.kwargs["tenant_id"] for call in dispatch.call_args_list] == [101, 202]
    assert all(call.kwargs["intent_secret"] == SECRET for call in dispatch.call_args_list)
    assert all(
        call.kwargs["registry"] is worker.DOMAIN_EFFECT_WORKER_REGISTRY
        for call in dispatch.call_args_list
    )
    assert report["processed"] == 2
    assert report["succeeded"] == 2
    assert [item["tenant_id"] for item in report["tenants"]] == [101, 202]


def test_queue_worker_reserves_batch_capacity_when_first_tenant_is_saturated(
    app,
    monkeypatch,
):
    _queue(app, monkeypatch, "303,101,202")

    def saturated_dispatch(**kwargs):
        processed = kwargs["limit"]
        return DomainEffectDispatchSummary(
            processed=processed,
            succeeded=processed,
            skipped=0,
            unknown=0,
            retry_wait=0,
            dead=0,
        )

    with app.app_context(), patch.object(
        worker,
        "dispatch_domain_effects",
        side_effect=saturated_dispatch,
    ) as dispatch:
        report = worker.dispatch_domain_effect_batch(limit=6)

    assert [call.kwargs["tenant_id"] for call in dispatch.call_args_list] == [
        101,
        202,
        303,
    ]
    assert [call.kwargs["limit"] for call in dispatch.call_args_list] == [2, 2, 2]
    assert report["processed"] == 6
    assert [item["processed"] for item in report["tenants"]] == [2, 2, 2]


def test_queue_worker_rotates_canary_first_choice_when_batch_is_smaller(
    app,
    monkeypatch,
):
    _queue(app, monkeypatch, "3,1,2")
    summary = DomainEffectDispatchSummary(
        processed=1,
        succeeded=1,
        skipped=0,
        unknown=0,
        retry_wait=0,
        dead=0,
    )

    with app.app_context(), patch.object(
        worker,
        "dispatch_domain_effects",
        return_value=summary,
    ) as dispatch:
        reports = [worker.dispatch_domain_effect_batch(limit=1) for _ in range(3)]

    assert [call.kwargs["tenant_id"] for call in dispatch.call_args_list] == [1, 2, 3]
    assert [report["tenants"][0]["tenant_id"] for report in reports] == [1, 2, 3]
    assert all(report["processed"] == 1 for report in reports)


def test_deadline_stops_claiming_new_domain_tenants_and_reaches_dispatch_loop(
    app,
    monkeypatch,
):
    _queue(app, monkeypatch, "101,202")
    app.extensions.pop(worker._ROUND_ROBIN_EXTENSION_KEY, None)
    clock = Mock(side_effect=[0.0, 2.0])
    summary = DomainEffectDispatchSummary(
        processed=1,
        succeeded=1,
        skipped=0,
        unknown=0,
        retry_wait=0,
        dead=0,
    )

    with app.app_context(), patch.object(
        worker,
        "dispatch_domain_effects",
        return_value=summary,
    ) as dispatch:
        report = worker.dispatch_domain_effect_batch(
            limit=2,
            deadline_monotonic=1.0,
            clock=clock,
        )

    dispatch.assert_called_once()
    assert callable(dispatch.call_args.kwargs["should_continue"])
    assert report["processed"] == 1
    assert [item["tenant_id"] for item in report["tenants"]] == [101]


def test_celery_wakeup_is_post_commit_hint_and_canary_scoped(app, monkeypatch):
    _queue(app, monkeypatch, "101")
    monkeypatch.setitem(app.config, "TESTING", False)
    monkeypatch.setitem(app.config, "DOMAIN_EFFECT_OUTBOX_CELERY_WAKEUP_ENABLED", True)

    with app.app_context(), patch.object(
        worker.dispatch_domain_effects_task,
        "apply_async",
    ) as apply_async:
        assert worker.enqueue_domain_effect_dispatch(tenant_id=101) is True
        assert worker.enqueue_domain_effect_dispatch(tenant_id=202) is False

    apply_async.assert_called_once_with(
        args=[101],
        retry=False,
        ignore_result=True,
    )
