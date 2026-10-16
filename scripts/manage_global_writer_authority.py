"""Inspect and CAS-transition the shared Render/Vercel writer authority.

The database URL is accepted only through a named environment variable.  All
mutations require the global authority gate and the local writer fence to be
enabled in this process.  Owner transfer is possible only while PostgreSQL
records both runtimes as fenced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cutover_writer_fence import cutover_writer_fence_enabled
from global_writer_authority import (
    GLOBAL_WRITER_AUTHORITY_CONTRACT,
    GLOBAL_WRITER_AUTHORITY_DATABASE_URL,
    configured_writer_runtime,
    global_writer_authority_enabled,
    writer_authority_endpoint_is_recognizably_pooled,
)
from services.global_writer_authority import (
    GlobalWriterAuthorityState,
    GlobalWriterAuthorityTransitionError,
    activate_global_writer_owner,
    attest_runtime_fenced,
    bootstrap_global_writer_owner,
    load_global_writer_authority_state,
    transfer_global_writer_owner,
)


ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def _state_payload(state: GlobalWriterAuthorityState) -> dict[str, Any]:
    return {
        "owner_runtime": state.owner_runtime,
        "epoch": state.epoch,
        "render_fenced": state.render_fenced,
        "vercel_fenced": state.vercel_fenced,
    }


def _require_mutation_environment(environ: Mapping[str, str]) -> str:
    if not global_writer_authority_enabled(environ):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_not_enabled"
        )
    if not cutover_writer_fence_enabled(environ):
        raise GlobalWriterAuthorityTransitionError(
            "local_writer_fence_required_for_transition"
        )
    runtime = configured_writer_runtime(environ)
    if runtime is None:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_runtime_identity_invalid"
        )
    return runtime


def execute_authority_command(
    executor: Any,
    *,
    command: str,
    environ: Mapping[str, str],
    expected_epoch: int | None = None,
    target_runtime: str | None = None,
) -> dict[str, Any]:
    if command == "status":
        state = load_global_writer_authority_state(executor)
        return {
            "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
            "status": "observed",
            "changed": False,
            "authority": _state_payload(state),
        }

    runtime = _require_mutation_environment(environ)
    if expected_epoch is None:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_epoch_required"
        )
    if command == "attest-fenced":
        state = attest_runtime_fenced(
            executor,
            runtime=runtime,
            expected_epoch=expected_epoch,
        )
    elif command == "bootstrap":
        if target_runtime != runtime:
            raise GlobalWriterAuthorityTransitionError(
                "global_writer_authority_bootstrap_identity_mismatch"
            )
        state = bootstrap_global_writer_owner(
            executor,
            runtime=runtime,
            expected_epoch=expected_epoch,
        )
    elif command == "transfer":
        state = transfer_global_writer_owner(
            executor,
            from_runtime=runtime,
            to_runtime=str(target_runtime or ""),
            expected_epoch=expected_epoch,
        )
    elif command == "activate":
        if target_runtime not in {None, runtime}:
            raise GlobalWriterAuthorityTransitionError(
                "global_writer_authority_activation_identity_mismatch"
            )
        state = activate_global_writer_owner(
            executor,
            runtime=runtime,
            expected_epoch=expected_epoch,
        )
    else:
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_command_invalid"
        )
    return {
        "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
        "status": "transitioned",
        "changed": True,
        "command": command,
        "authority": _state_payload(state),
    }


def _database_url(environ: Mapping[str, str], variable_name: str | None) -> str:
    normalized_name = str(variable_name or "").strip()
    if (
        not ENVIRONMENT_NAME_PATTERN.fullmatch(normalized_name)
        or normalized_name != GLOBAL_WRITER_AUTHORITY_DATABASE_URL
    ):
        raise GlobalWriterAuthorityTransitionError(
            "database_environment_variable_name_invalid"
        )
    value = str(environ.get(normalized_name) or "").strip()
    if not value:
        raise GlobalWriterAuthorityTransitionError(
            "database_environment_variable_missing"
        )
    try:
        url = make_url(value)
    except Exception as exc:
        raise GlobalWriterAuthorityTransitionError(
            "database_url_invalid"
        ) from exc
    if not url.drivername.startswith("postgresql"):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_requires_postgresql"
        )
    if not url.host or not url.username or not url.password or not url.database:
        raise GlobalWriterAuthorityTransitionError(
            "database_url_incomplete"
        )
    indirect_parameters = {
        "host",
        "hostaddr",
        "options",
        "passfile",
        "password",
        "service",
        "servicefile",
        "user",
    }.intersection(str(key).lower() for key in url.query)
    if indirect_parameters:
        raise GlobalWriterAuthorityTransitionError("database_url_invalid")
    if writer_authority_endpoint_is_recognizably_pooled(url.host, url.query):
        raise GlobalWriterAuthorityTransitionError(
            "global_writer_authority_control_database_must_be_direct"
        )
    if str(url.query.get("sslmode") or "").strip().lower() not in {
        "require",
        "verify-ca",
        "verify-full",
    }:
        raise GlobalWriterAuthorityTransitionError("database_tls_required")
    return value


class _RedactedArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise GlobalWriterAuthorityTransitionError("command_arguments_invalid")


def _parser() -> argparse.ArgumentParser:
    parser = _RedactedArgumentParser(description=__doc__)
    parser.add_argument(
        "--environment-variable",
        default=GLOBAL_WRITER_AUTHORITY_DATABASE_URL,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    for name in ("attest-fenced", "bootstrap", "transfer", "activate"):
        command = subparsers.add_parser(name)
        command.add_argument("--expected-epoch", required=True, type=int)
        if name in {"bootstrap", "transfer"}:
            command.add_argument(
                "--target-runtime",
                required=True,
                choices=("render", "vercel"),
            )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    runtime_env = os.environ if environ is None else environ
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        database_url = _database_url(runtime_env, args.environment_variable)
        engine = create_engine(
            make_url(database_url).set(drivername="postgresql+psycopg"),
            poolclass=NullPool,
            connect_args={
                "application_name": "chatboc_global_writer_authority",
                "connect_timeout": 10,
            },
        )
        try:
            with engine.connect() as connection:
                with connection.begin():
                    transaction_mode = (
                        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY"
                        if args.command == "status"
                        else "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
                    )
                    connection.exec_driver_sql(transaction_mode)
                    connection.exec_driver_sql("SET LOCAL statement_timeout = '15000ms'")
                    connection.exec_driver_sql("SET LOCAL lock_timeout = '5000ms'")
                    connection.exec_driver_sql(
                        "SET LOCAL idle_in_transaction_session_timeout = '15000ms'"
                    )
                    payload = execute_authority_command(
                        connection,
                        command=args.command,
                        environ=runtime_env,
                        expected_epoch=getattr(args, "expected_epoch", None),
                        target_runtime=getattr(args, "target_runtime", None),
                    )
        finally:
            engine.dispose()
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        return 0
    except GlobalWriterAuthorityTransitionError as exc:
        print(
            json.dumps(
                {
                    "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
                    "status": "blocked",
                    "changed": False,
                    "reason_code": exc.reason_code,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "contract_version": GLOBAL_WRITER_AUTHORITY_CONTRACT,
                    "status": "blocked",
                    "changed": False,
                    "reason_code": "global_writer_authority_runtime_failed",
                    "error_type": type(exc).__name__,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
