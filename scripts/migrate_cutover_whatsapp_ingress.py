"""Initialize/verify the independent WhatsApp cutover ingress database."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cutover_ingress.core import (
    CutoverIngressConfigurationError,
    CutoverIngressSettings,
    _database_identity,
    _normalize_database_driver,
    _strict_database_url,
)
from cutover_ingress.migration import (
    assert_cutover_ingress_schema,
    migrate_cutover_ingress,
)
from cutover_ingress.schema import buffered_whatsapp_ingress


def _direct_migration_url(
    runtime_database_url: str,
    environ: Mapping[str, str],
) -> str:
    direct_url = str(
        environ.get("CUTOVER_INGRESS_MIGRATIONS_DATABASE_URL") or ""
    ).strip()
    if not direct_url:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_migration_database_url_missing"
        )
    try:
        parsed = _strict_database_url(direct_url)
    except CutoverIngressConfigurationError as exc:
        if str(exc) == "cutover_ingress_database_url_invalid":
            raise CutoverIngressConfigurationError(
                "cutover_ingress_migration_database_url_invalid"
            ) from exc
        raise
    host = str(parsed.host or "").lower()
    if re.search(r"(?:^|[-.])pooler(?=\.|$)", host):
        raise CutoverIngressConfigurationError(
            "cutover_ingress_migration_database_must_be_direct"
        )
    sslmode = str(parsed.query.get("sslmode") or "").strip().lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise CutoverIngressConfigurationError(
            "cutover_ingress_migration_database_tls_required"
        )
    if _database_identity(runtime_database_url) != _database_identity(direct_url):
        raise CutoverIngressConfigurationError(
            "cutover_ingress_runtime_migration_database_mismatch"
        )
    return _normalize_database_driver(direct_url)


def _assert_runtime_pooled_contract(settings: CutoverIngressSettings) -> None:
    """Prove schema and INSERT/rollback privileges through the runtime DSN."""

    engine = settings.build_engine()
    probe_sid = "SM" + uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    try:
        assert_cutover_ingress_schema(engine)
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                connection.execute(
                    buffered_whatsapp_ingress.insert().values(
                        message_sid=probe_sid,
                        provider="twilio",
                        account_sid=settings.twilio_account_sid,
                        tenant_id=settings.tenant_id,
                        stream_key="0" * 64,
                        payload_digest="1" * 64,
                        encryption_key_id=settings.active_encryption_key_id,
                        nonce=b"0" * 12,
                        ciphertext=b"runtime-write-probe",
                        envelope_hmac="2" * 64,
                        status="buffered",
                        attempt_count=0,
                        max_attempts=settings.max_attempts,
                        available_at=now,
                        received_at=now,
                        row_version=1,
                        contract_version="chatboc.cutover_ingress.probe.v1",
                        created_at=now,
                        updated_at=now,
                    )
                )
            finally:
                transaction.rollback()
        with engine.connect() as connection:
            if connection.scalar(
                select(buffered_whatsapp_ingress.c.message_sid).where(
                    buffered_whatsapp_ingress.c.message_sid == probe_sid
                )
            ):
                raise CutoverIngressConfigurationError(
                    "cutover_ingress_runtime_probe_rollback_failed"
                )
    finally:
        engine.dispose()


def main() -> int:
    settings = CutoverIngressSettings.from_environ()
    migration_url = _direct_migration_url(settings.database_url, os.environ)
    engine = create_engine(
        migration_url,
        future=True,
        poolclass=NullPool,
        hide_parameters=True,
        connect_args={
            "application_name": "chatboc_cutover_ingress_migration",
            "connect_timeout": 10,
        },
    )
    try:
        revision = migrate_cutover_ingress(engine)
    finally:
        engine.dispose()
    _assert_runtime_pooled_contract(settings)
    print(
        json.dumps(
            {
                "contract_version": "chatboc.cutover_ingress.migration.v1",
                "database_url_exposed": False,
                "migration_connection": "direct",
                "revision": revision,
                "runtime_connection": "pooled_verified",
                "status": "buffer_ready",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
