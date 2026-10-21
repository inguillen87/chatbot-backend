import json
from unittest.mock import call, patch

import pytest
from flask import Flask

from cli_commands import register_commands
from services.global_writer_authority import GlobalWriterAuthorityDecision
from services.survey_response_effects import dispatch_survey_response_effects


def _dispatch_result(
    *,
    claimed=0,
    processed=None,
    succeeded=None,
    skipped=0,
    retry_wait=0,
    dead=0,
    fenced=0,
    **extra,
):
    if processed is None:
        processed = claimed
    if succeeded is None:
        succeeded = processed - skipped - retry_wait - dead
    return {
        "contract_version": "surveys.response_effect.v1",
        "claimed": claimed,
        "processed": processed,
        "succeeded": succeeded,
        "skipped": skipped,
        "retry_wait": retry_wait,
        "dead": dead,
        "fenced": fenced,
        **extra,
    }


def _tenant_summary(*, tenant_id=7, dead=0):
    return {
        "contract_version": "surveys.response_effect_summary.v1",
        "tenant_id": tenant_id,
        "total": dead,
        "due": 0,
        "in_flight": 0,
        "dead": dead,
        "oldest_due_available_at": None,
        "by_status": {"dead": dead},
        "by_effect_type": {},
    }


@pytest.fixture()
def cli_app():
    app = Flask("survey-effect-cli-tests")
    app.config.update(TESTING=True)
    register_commands(app)
    return app


def _invoke(cli_app, *args):
    return cli_app.test_cli_runner().invoke(
        args=["dispatch-survey-response-effects", *args]
    )


def _payload(result):
    return json.loads(result.output.strip())


@pytest.mark.parametrize(
    "args",
    [
        (),
        ("--all-tenants", "--tenant-id", "7"),
    ],
)
def test_cli_requires_exactly_one_explicit_scope(cli_app, args):
    with patch(
        "services.survey_response_effects.dispatch_survey_response_effects"
    ) as dispatch:
        result = _invoke(cli_app, *args)

    assert result.exit_code == 2
    assert result.exception is not None
    assert _payload(result) == {
        "batches": 0,
        "command": "dispatch-survey-response-effects",
        "contract_version": "cli.survey_response_effect_dispatch.v1",
        "exit_policy": {"dead_effects_exit_code": 3, "fail_on_dead": True},
        "limits": {"batch_size": 50, "max_batches": 10, "max_effects": 500},
        "outbox_after": None,
        "reason_code": "exactly_one_scope_required",
        "scope": {
            "mode": "invalid",
            "tenant_id": 7 if "--tenant-id" in args else None,
        },
        "status": "rejected",
        "termination_reason": "invalid_arguments",
        "totals": {
            "claimed": 0,
            "dead": 0,
            "fenced": 0,
            "processed": 0,
            "retry_wait": 0,
            "skipped": 0,
            "succeeded": 0,
        },
    }
    dispatch.assert_not_called()


def test_tenant_cli_honors_effect_cap_and_does_not_emit_batch_payload(cli_app):
    first = _dispatch_result(
        claimed=2,
        internal_note="citizen@example.test +5491112345678",
    )
    second = _dispatch_result(claimed=1)

    with (
        patch("cli_commands._survey_effect_tenant_exists", return_value=True),
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            side_effect=[first, second],
        ) as dispatch,
        patch(
            "services.survey_response_effects.summarize_survey_response_effects",
            return_value=_tenant_summary(),
        ) as summarize,
    ):
        result = _invoke(
            cli_app,
            "--tenant-id",
            "7",
            "--batch-size",
            "2",
            "--max-batches",
            "10",
            "--max-effects",
            "3",
        )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["status"] == "completed"
    assert payload["termination_reason"] == "max_effects"
    assert payload["batches"] == 2
    assert payload["totals"] == {
        "claimed": 3,
        "dead": 0,
        "fenced": 0,
        "processed": 3,
        "retry_wait": 0,
        "skipped": 0,
        "succeeded": 3,
    }
    assert "citizen@example.test" not in result.output
    assert "+5491112345678" not in result.output
    assert dispatch.call_args_list == [
        call(tenant_id=7, limit=2),
        call(tenant_id=7, limit=1),
    ]
    summarize.assert_called_once_with(7)


def test_global_cli_stops_when_dispatcher_reports_no_claims(cli_app):
    with (
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            side_effect=[
                _dispatch_result(claimed=1, succeeded=0, retry_wait=1),
                _dispatch_result(),
            ],
        ) as dispatch,
        patch("cli_commands._count_dead_survey_response_effects", return_value=0),
    ):
        result = _invoke(
            cli_app,
            "--all-tenants",
            "--batch-size",
            "4",
            "--max-batches",
            "5",
            "--max-effects",
            "12",
        )

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["scope"] == {"mode": "global", "tenant_id": None}
    assert payload["termination_reason"] == "drained"
    assert payload["batches"] == 2
    assert payload["totals"]["claimed"] == 1
    assert payload["totals"]["retry_wait"] == 1
    assert payload["outbox_after"] == {"scope": "global", "dead": 0}
    assert dispatch.call_args_list == [
        call(tenant_id=None, limit=4),
        call(tenant_id=None, limit=4),
    ]


def test_cli_rejects_unknown_tenant_before_dispatch(cli_app):
    with (
        patch("cli_commands._survey_effect_tenant_exists", return_value=False),
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects"
        ) as dispatch,
    ):
        result = _invoke(cli_app, "--tenant-id", "999")

    assert result.exit_code == 2
    payload = _payload(result)
    assert payload["status"] == "rejected"
    assert payload["reason_code"] == "tenant_not_found"
    dispatch.assert_not_called()


def test_global_authority_blocks_cli_before_scope_query_claim_or_effect(cli_app):
    cli_app.config.update(
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
    )
    denied = GlobalWriterAuthorityDecision(
        allowed=False,
        enabled=True,
        reason_code="runtime_not_global_writer_owner",
        epoch=17,
    )

    with (
        patch(
            "services.global_writer_authority.evaluate_global_writer_authority",
            return_value=denied,
        ) as evaluate,
        patch("cli_commands._survey_effect_tenant_exists") as tenant_query,
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects"
        ) as dispatch,
        patch(
            "services.survey_response_effects.summarize_survey_response_effects"
        ) as summarize,
        patch("cli_commands._count_dead_survey_response_effects") as dead_query,
    ):
        result = _invoke(cli_app, "--tenant-id", "7")

    assert result.exit_code == 0
    payload = _payload(result)
    assert payload["contract_version"] == "cutover.global_writer_authority.v1"
    assert payload["status"] == "fenced"
    assert payload["reason_code"] == "runtime_not_global_writer_owner"
    assert payload["batches"] == 0
    assert all(value == 0 for value in payload["totals"].values())
    evaluate.assert_called_once()
    tenant_query.assert_not_called()
    dispatch.assert_not_called()
    summarize.assert_not_called()
    dead_query.assert_not_called()


def test_global_authority_blocks_direct_dispatch_before_query_claim_or_effect(
    cli_app,
):
    cli_app.config.update(
        CUTOVER_WRITER_FENCE_ENABLED=False,
        CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True,
        CUTOVER_RUNTIME_IDENTITY="vercel",
    )
    denied = GlobalWriterAuthorityDecision(
        allowed=False,
        enabled=True,
        reason_code="runtime_not_global_writer_owner",
        epoch=18,
    )

    with (
        cli_app.app_context(),
        patch(
            "services.global_writer_authority.evaluate_global_writer_authority",
            return_value=denied,
        ) as evaluate,
        patch("services.survey_response_effects.db.session.query") as query,
        patch("services.survey_response_effects._claim_effect") as claim,
        patch("services.survey_response_effects._execute_effect") as execute,
    ):
        report = dispatch_survey_response_effects(tenant_id=7, limit=3)

    assert report["contract_version"] == "cutover.global_writer_authority.v1"
    assert report["status"] == "fenced"
    assert report["reason_code"] == "runtime_not_global_writer_owner"
    assert report["claimed"] == 0
    assert report["processed"] == 0
    evaluate.assert_called_once()
    query.assert_not_called()
    claim.assert_not_called()
    execute.assert_not_called()


@pytest.mark.parametrize(
    ("extra_args", "expected_exit"),
    [
        ((), 3),
        (("--allow-dead",), 0),
    ],
)
def test_dead_effect_exit_policy_is_explicit_and_overridable(
    cli_app, extra_args, expected_exit
):
    with (
        patch("cli_commands._survey_effect_tenant_exists", return_value=True),
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            return_value=_dispatch_result(),
        ),
        patch(
            "services.survey_response_effects.summarize_survey_response_effects",
            return_value=_tenant_summary(dead=2),
        ),
    ):
        result = _invoke(cli_app, "--tenant-id", "7", *extra_args)

    assert result.exit_code == expected_exit
    payload = _payload(result)
    assert payload["status"] == "completed_with_dead"
    assert payload["outbox_after"]["dead"] == 2
    assert payload["exit_policy"]["fail_on_dead"] is (expected_exit == 3)


def test_unhandled_failure_is_sanitized_json_and_rolls_back(cli_app):
    unsafe_failure = RuntimeError("citizen@example.test +5491112345678")
    with (
        patch("cli_commands._survey_effect_tenant_exists", return_value=True),
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            side_effect=unsafe_failure,
        ),
        patch("cli_commands._rollback_survey_effect_cli_session") as rollback,
    ):
        result = _invoke(cli_app, "--tenant-id", "7")

    assert result.exit_code == 1
    payload = _payload(result)
    assert payload["status"] == "failed"
    assert payload["reason_code"] == "dispatch_failed"
    assert payload["error_type"] == "RuntimeError"
    assert "citizen@example.test" not in result.output
    assert "+5491112345678" not in result.output
    rollback.assert_called_once_with()


def test_malformed_dispatch_counters_fail_instead_of_looping(cli_app):
    malformed = _dispatch_result(claimed=1, processed=2, succeeded=2)
    with (
        patch("cli_commands._survey_effect_tenant_exists", return_value=True),
        patch(
            "services.survey_response_effects.dispatch_survey_response_effects",
            return_value=malformed,
        ) as dispatch,
        patch("cli_commands._rollback_survey_effect_cli_session"),
    ):
        result = _invoke(cli_app, "--tenant-id", "7", "--max-batches", "100")

    assert result.exit_code == 1
    assert _payload(result)["reason_code"] == "dispatch_failed"
    dispatch.assert_called_once_with(tenant_id=7, limit=50)


def test_help_documents_scope_and_exit_codes(cli_app):
    result = _invoke(cli_app, "--help")
    normalized_help = " ".join(result.output.split())

    assert result.exit_code == 0
    assert "Exactly one scope is mandatory" in normalized_help
    assert "0 completed" in normalized_help
    assert "1 operational failure" in normalized_help
    assert "2 invalid arguments/scope" in normalized_help
    assert "3 dead effects" in normalized_help
