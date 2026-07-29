from unittest.mock import patch

import pytest

from celery_utils import celery_app
from services import tasks


class RetryRaised(Exception):
    pass


def test_survey_response_effect_task_is_registered_with_safe_delivery_options():
    task = tasks.dispatch_survey_response_effects_task

    assert task.name == "tasks.dispatch_survey_response_effects"
    assert celery_app.tasks[task.name].name == task.name
    assert task.max_retries == 4
    assert task.default_retry_delay == 60
    assert task.acks_late is True


def test_survey_response_effect_task_scopes_and_bounds_dispatch():
    dispatcher_result = {
        "contract_version": "surveys.response_effect.v1",
        "claimed": 3,
        "processed": 3,
        "succeeded": 2,
        "skipped": 1,
        "retry_wait": 0,
        "dead": 0,
        "fenced": 0,
    }

    with patch(
        "services.survey_response_effects.dispatch_survey_response_effects",
        return_value=dispatcher_result,
    ) as dispatch:
        result = tasks.dispatch_survey_response_effects_task.run("7", limit=10_000)

    dispatch.assert_called_once_with(tenant_id=7, limit=100)
    assert result == {
        "contract_version": "tasks.dispatch_survey_response_effects.v1",
        "status": "completed",
        "tenant_id": 7,
        "limit": 100,
        "dispatcher": dispatcher_result,
        "task_id": None,
        "attempt": 1,
    }


@pytest.mark.parametrize("tenant_id", [None, True, 0, -1, "invalid", 1.5])
def test_survey_response_effect_task_rejects_invalid_tenant_without_retry(tenant_id):
    with (
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects"
        ) as dispatch,
        patch.object(tasks.dispatch_survey_response_effects_task, "retry") as retry,
    ):
        result = tasks.dispatch_survey_response_effects_task.run(tenant_id)

    dispatch.assert_not_called()
    retry.assert_not_called()
    assert result["status"] == "rejected"
    assert result["reason_code"] == "tenant_id_invalid"
    assert result["retryable"] is False
    assert result["tenant_id"] is None


@pytest.mark.parametrize(
    ("raw_limit", "expected"),
    [
        (None, 50),
        (True, 50),
        ("invalid", 50),
        (0, 1),
        (-10, 1),
        (8, 8),
        (101, 100),
    ],
)
def test_survey_response_effect_task_limit_is_always_bounded(raw_limit, expected):
    assert tasks._bounded_survey_response_effect_limit(raw_limit) == expected


def test_survey_response_effect_task_rolls_back_before_retrying():
    events = []
    failure = RuntimeError("database unavailable")

    def record_rollback():
        events.append("rollback")

    def record_retry(*, exc):
        assert exc is failure
        events.append("retry")
        raise RetryRaised

    with (
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            side_effect=failure,
        ) as dispatch,
        patch(
            "services.tasks._rollback_survey_response_effect_task_session",
            side_effect=record_rollback,
        ) as rollback,
        patch.object(
            tasks.dispatch_survey_response_effects_task,
            "retry",
            side_effect=record_retry,
        ) as retry,
    ):
        with pytest.raises(RetryRaised):
            tasks.dispatch_survey_response_effects_task.run(tenant_id=9, limit=12)

    dispatch.assert_called_once_with(tenant_id=9, limit=12)
    rollback.assert_called_once_with()
    retry.assert_called_once_with(exc=failure)
    assert events == ["rollback", "retry"]
