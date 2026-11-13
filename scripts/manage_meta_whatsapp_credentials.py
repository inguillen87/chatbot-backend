"""Administrative Meta installer. Default: provider GETs + read-only DB plan.

Token input is a non-echoing terminal or an explicitly supplied protected stdin
pipe. No token argument, token environment variable, public HTTP endpoint,
provider write, automatic retry, message send or dotenv loading is supported.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import time
import warnings

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class InputError(RuntimeError):
    pass


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, which could be a token
        # accidentally supplied through an unsupported --token option.
        raise InputError("meta_operation_arguments_invalid")


def protected_stdin(stream):
    try:
        mode = os.fstat(stream.fileno()).st_mode
        return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)
    except (AttributeError, OSError, ValueError):
        return False


def parser():
    result = SafeParser(description=__doc__)
    result.add_argument("--target", required=True, choices=("junin-production", "tdf-sandbox"))
    result.add_argument("--actor-user-id", type=int, required=True)
    result.add_argument("--expected-revision", type=int, required=True)
    result.add_argument("--expires-at", type=int, required=True, help="Verified finite Unix UTC expiry")
    result.add_argument("--expected-writer-epoch", type=int, required=True)
    result.add_argument("--database-environment-variable", default="MIGRATIONS_DATABASE_URL")
    result.add_argument("--expected-database-identity-sha256")
    result.add_argument("--token-stdin", action="store_true", help="Read one token from a protected noninteractive pipe")
    result.add_argument("--apply", action="store_true")
    result.add_argument("--approved-plan-sha256")
    return result


def read_token(*, from_stdin=False, stream=None, prompt=getpass.getpass):
    stream = sys.stdin if stream is None else stream
    if from_stdin:
        if stream.isatty():
            raise InputError("meta_operation_protected_stdin_required")
        value = stream.read(4099)
        if not isinstance(value, str):
            raise InputError("meta_operation_token_input_invalid")
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    else:
        if not stream.isatty():
            raise InputError("meta_operation_hidden_terminal_required")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = prompt("New Meta access token (hidden): ")
            except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
                raise InputError("meta_operation_hidden_input_unavailable") from None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{16,4096}", value):
        raise InputError("meta_operation_token_input_invalid")
    return value


def _database(database_url):
    from sqlalchemy.engine import make_url
    try:
        url = make_url(database_url)
        allowed_query = {"sslmode", "channel_binding", "sslrootcert"}
        if (url.get_backend_name() != "postgresql" or not url.host or not url.database
                or not re.fullmatch(r"[A-Za-z0-9.-]+", url.host)
                or set(url.query) - allowed_query
                or any(not isinstance(value, str) for value in url.query.values())
                or url.query.get("sslmode") not in {"require", "verify-ca", "verify-full"}):
            raise ValueError
        identity = {"backend": "postgresql", "host": url.host.lower(),
                    "port": url.port or 5432, "database": url.database}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return url.set(drivername="postgresql+psycopg", port=url.port or 5432), fingerprint
    except Exception:
        raise InputError("meta_operation_postgresql_tls_required") from None


def _run(args, token):
    # Process-only bootstrap controls. No Vercel/env mutation or dotenv reads.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["FLASK_SKIP_GLOBAL_APP"] = "1"
    from flask import Flask
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool
    from config import Config
    from services.global_writer_authority import global_writer_authority_lease
    from services.meta_credential_operations import OperationRequest, prepare_operation, apply_operation, _require

    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", args.database_environment_variable):
        raise InputError("meta_operation_database_environment_name_invalid")
    if any(os.environ.get(name) for name in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE", "PGOPTIONS")):
        raise InputError("meta_operation_database_indirection_blocked")
    url, fingerprint = _database(os.environ.get(args.database_environment_variable, ""))
    if args.expected_database_identity_sha256 is not None and args.expected_database_identity_sha256 != fingerprint:
        raise InputError("meta_operation_database_identity_mismatch")
    if args.apply and (not args.approved_plan_sha256 or not args.expected_database_identity_sha256):
        raise InputError("meta_operation_apply_approval_required")
    app = Flask("meta-credential-administrator")
    app.config.from_object(Config)
    request = OperationRequest(args.target, args.actor_user_id, args.expected_revision,
                               args.expires_at, fingerprint, args.expected_writer_epoch)
    engine = create_engine(url, poolclass=NullPool, hide_parameters=True,
        connect_args={"application_name": "chatboc_meta_credential_ops", "connect_timeout": 10})
    try:
        with app.app_context():
            with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
                with connection.begin() as transaction:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    connection.execute(text("SET LOCAL search_path = public"))
                    connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                    _require(connection.execute(text("SELECT current_database()")).scalar_one() == url.database,
                             "meta_operation_database_identity_mismatch")
                    with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                        prepared = prepare_operation(session, request, token, app.config)
                    transaction.rollback()
            if not args.apply:
                return prepared.public_payload()
            # No network calls or token prompts occur while holding writer lease.
            # This existing lease mode disables the control connection's short
            # idle TTL so its FOR SHARE remains held through the actual commit.
            with global_writer_authority_lease(app.config, request_lifetime=True) as lease:
                with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
                    with connection.begin():
                        connection.execute(text("SET LOCAL search_path = public"))
                        connection.execute(text("SET LOCAL statement_timeout = '10s'"))
                        connection.execute(text("SET LOCAL lock_timeout = '3s'"))
                        _require(connection.execute(text("SELECT current_database()")).scalar_one() == url.database,
                                 "meta_operation_database_identity_mismatch")
                        with Session(bind=connection, autoflush=False, expire_on_commit=False) as session:
                            result = apply_operation(session, prepared, token, app.config,
                                approved_plan_sha256=args.approved_plan_sha256, writer_lease=lease)
                            final = lease.revalidate(app.config)
                            _require(final.allowed and final.enabled and final.epoch == request.expected_writer_epoch,
                                     "meta_operation_writer_authority_denied")
                            _require(prepared.observed_at <= int(time.time()) < prepared.valid_until,
                                     "meta_operation_provider_proof_expired")
                result["writes_performed"] = True
                result["committed"] = True
                return result
    finally:
        engine.dispose()


def main(argv=None):
    # Third-party exceptions/SQL/HTTP debug logs must not disclose the input or
    # encrypted envelope. Structured audit remains in the same DB transaction.
    logging.disable(logging.CRITICAL)
    token = None
    args = None
    try:
        args = parser().parse_args(argv)
        if args.token_stdin and not protected_stdin(sys.stdin):
            raise InputError("meta_operation_protected_stdin_required")
        token = read_token(from_stdin=args.token_stdin)
        result = _run(args, token)
        print(json.dumps(result, sort_keys=True, ensure_ascii=True))
        return 0
    except InputError as exc:
        reason = str(exc)
    except Exception as exc:
        # Do not import again on a failed bootstrap: even missing dependencies
        # must yield a fixed code rather than an unhandled traceback.
        safe_types = {
            ("services.meta_credential_operations", "MetaCredentialOperationError"),
            ("services.meta_whatsapp_runtime", "MetaRuntimeError"),
            ("services.tenant_provider_credentials", "ProviderCredentialError"),
        }
        value = getattr(exc, "code", str(exc))
        reason = value if ((type(exc).__module__, type(exc).__name__) in safe_types
                           and isinstance(value, str)
                           and re.fullmatch(r"[a-z][a-z0-9_]{1,100}", value)) else "meta_operation_failed"
    finally:
        # Python cannot promise erasure of immutable strings; lifetime is bounded
        # to this administrative process, with no persistence or diagnostic dump.
        token = None
    print(json.dumps({"contract": "chatboc.meta_credential_operation.v1", "ok": False,
                      "reason_code": reason, "messages_sent": False, "provider_mutations": False,
                      "database_outcome": "unverified" if args is not None and args.apply else "no_apply_requested"}))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
