import builtins
import os
import sys
import threading
from unittest.mock import patch

from flask import Flask
import pytest

from services import whatsapp_inbound_worker as worker


def _app(**overrides):
    app = Flask(__name__)
    app.config.update(
        WHATSAPP_INBOUND_DURABILITY_MODE="legacy",
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS="",
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=False,
        WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=True,
        WHATSAPP_INBOUND_DEAD_PAYLOAD_RETENTION_HOURS=72,
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_BATCH_SIZE=200,
    )
    app.config.update(overrides)
    return app


def test_legacy_standby_has_no_db_queue_or_provider_work():
    app = _app()
    stop = threading.Event()
    stop.set()

    with (
        patch.object(worker, "summarize_whatsapp_turn_health") as health,
        patch.object(worker, "process_whatsapp_inbound_stream") as inbound,
        patch.object(worker, "dispatch_whatsapp_outbound_attempts") as outbound,
        patch.object(worker, "Client") as provider,
    ):
        report = worker.run_whatsapp_durable_worker(
            app,
            stop_event=stop,
            standby_when_legacy=True,
        )

    assert report["status"] == "standby"
    assert report["mode"] == "legacy"
    assert report["cycles"] == 0
    health.assert_not_called()
    inbound.assert_not_called()
    outbound.assert_not_called()
    provider.assert_not_called()


def test_legacy_worker_without_explicit_standby_still_fails_closed():
    with pytest.raises(
        RuntimeError,
        match="whatsapp_durable_worker_requires_queue_mode",
    ):
        worker.run_whatsapp_durable_worker(_app(), once=True)


def test_expired_deadline_does_not_claim_an_outbound_attempt():
    with patch.object(worker, "dispatch_next_whatsapp_outbound_attempt") as dispatch:
        report = worker.dispatch_whatsapp_outbound_attempts(
            limit=5,
            deadline_monotonic=10.0,
            clock=lambda: 10.0,
        )

    assert report == {
        "contract_version": "whatsapp.outbound_worker.v1",
        "processed": 0,
        "accepted": 0,
        "results": [],
    }
    dispatch.assert_not_called()


def test_legacy_standby_uses_one_unbounded_signal_wait_not_polling():
    class SignalWait:
        def __init__(self):
            self.wait_calls = []

        def is_set(self):
            return False

        def wait(self, timeout=None):
            self.wait_calls.append(timeout)
            return True

    stop = SignalWait()
    with (
        patch.object(worker, "summarize_whatsapp_turn_health") as health,
        patch.object(worker, "process_whatsapp_inbound_stream") as inbound,
        patch.object(worker, "dispatch_whatsapp_outbound_attempts") as outbound,
    ):
        report = worker.run_whatsapp_durable_worker(
            _app(),
            stop_event=stop,
            standby_when_legacy=True,
        )

    assert report["status"] == "standby"
    assert stop.wait_calls == [None]
    health.assert_not_called()
    inbound.assert_not_called()
    outbound.assert_not_called()


def test_standby_cli_returns_before_importing_application():
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app":
            raise AssertionError("standby must not import/create the application")
        return original_import(name, *args, **kwargs)

    with (
        patch.object(
            sys,
            "argv",
            ["whatsapp_inbound_worker", "--standby-when-legacy", "--once"],
        ),
        patch.dict(
            os.environ,
            {
                "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
                "WHATSAPP_DURABLE_WORKER_STANDBY_ENABLED": "true",
            },
        ),
        patch("builtins.__import__", side_effect=guarded_import),
    ):
        assert worker.main() == 0


@pytest.mark.parametrize(
    ("overrides", "expected_status"),
    [
        ({}, "disabled"),
        (
            {
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED": True,
                "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD": True,
            },
            "legal_hold",
        ),
        (
            {
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED": True,
                "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD": False,
            },
            "no_retention_tenants",
        ),
    ],
)
def test_blocked_payload_scrub_never_queries_or_deletes(overrides, expected_status):
    app = _app(**overrides)
    with (
        app.app_context(),
        patch.object(worker, "scrub_expired_whatsapp_inbound_payloads") as scrub,
    ):
        report = worker.run_whatsapp_inbound_payload_scrub()

    assert report["status"] == expected_status
    assert report["selected"] == 0
    assert report["scrubbed"] == 0
    scrub.assert_not_called()


def test_payload_scrub_is_bounded_and_scoped_to_canary_tenants():
    app = _app(
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS="11,7,11",
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
        WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=False,
    )
    reports = [
        {
            "selected": 2,
            "scrubbed": 2,
            "completed_scrubbed": 1,
            "dead_scrubbed": 1,
        },
        {
            "selected": 1,
            "scrubbed": 1,
            "completed_scrubbed": 1,
            "dead_scrubbed": 0,
        },
    ]
    with (
        app.app_context(),
        patch.object(
            worker,
            "scrub_expired_whatsapp_inbound_payloads",
            side_effect=reports,
        ) as scrub,
    ):
        report = worker.run_whatsapp_inbound_payload_scrub(limit=5)

    assert report == {
        "contract_version": "whatsapp.inbound_payload_scrub_batch.v1",
        "status": "completed",
        "source_contract_version": "whatsapp.inbound_payload_scrub_run.v1",
        "tenant_count": 2,
        "selected": 3,
        "scrubbed": 3,
        "completed_scrubbed": 2,
        "dead_scrubbed": 1,
        "batch_limit": 5,
        "batch_remaining": 2,
    }
    assert [call.kwargs["tenant_id"] for call in scrub.call_args_list] == [7, 11]
    assert [call.kwargs["limit"] for call in scrub.call_args_list] == [3, 3]


def test_scrub_rejects_invalid_canary_allowlist_before_io():
    app = _app(
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS="7,not-a-tenant",
        WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED=True,
        WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD=False,
    )
    with (
        app.app_context(),
        patch.object(worker, "scrub_expired_whatsapp_inbound_payloads") as scrub,
        pytest.raises(
            RuntimeError,
            match="whatsapp_inbound_tenant_ids_invalid",
        ),
    ):
        worker.run_whatsapp_inbound_payload_scrub()
    scrub.assert_not_called()


def test_disabled_scrub_cli_returns_before_importing_application(capsys):
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "app":
            raise AssertionError("blocked retention cron must not create the application")
        return original_import(name, *args, **kwargs)

    with (
        patch.object(
            sys,
            "argv",
            ["whatsapp_inbound_worker", "--scrub-expired-payloads"],
        ),
        patch.dict(
            os.environ,
            {
                "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_TENANT_IDS": "",
                "WHATSAPP_INBOUND_PAYLOAD_SCRUB_ENABLED": "false",
                "WHATSAPP_INBOUND_PAYLOAD_LEGAL_HOLD": "true",
            },
        ),
        patch("builtins.__import__", side_effect=guarded_import),
    ):
        assert worker.main() == 0

    report = capsys.readouterr().out
    assert '"status": "disabled"' in report
