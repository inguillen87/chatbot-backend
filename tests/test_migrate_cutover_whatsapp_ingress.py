from __future__ import annotations

import pytest

from cutover_ingress.core import CutoverIngressConfigurationError
from scripts.migrate_cutover_whatsapp_ingress import _direct_migration_url


RUNTIME_POOLED_URL = (
    "postgresql+psycopg://runtime:secret@ep-safe-pooler.us-east-2.aws.neon.tech/"
    "cutover_ingress?sslmode=require"
)
MIGRATION_DIRECT_URL = (
    "postgresql+psycopg://migration:secret@ep-safe.us-east-2.aws.neon.tech/"
    "cutover_ingress?sslmode=verify-full"
)


def test_migration_accepts_direct_sibling_for_exact_runtime_database() -> None:
    assert _direct_migration_url(
        RUNTIME_POOLED_URL,
        {"CUTOVER_INGRESS_MIGRATIONS_DATABASE_URL": MIGRATION_DIRECT_URL},
    ) == MIGRATION_DIRECT_URL


def test_migration_normalizes_standard_neon_url_to_bundled_psycopg_driver() -> None:
    standard_url = MIGRATION_DIRECT_URL.replace(
        "postgresql+psycopg://", "postgresql://"
    )
    resolved = _direct_migration_url(
        RUNTIME_POOLED_URL,
        {"CUTOVER_INGRESS_MIGRATIONS_DATABASE_URL": standard_url},
    )

    assert resolved.startswith("postgresql+psycopg://")


@pytest.mark.parametrize(
    ("migration_url", "error_code"),
    [
        ("", "cutover_ingress_migration_database_url_missing"),
        ("not-a-database-url", "cutover_ingress_migration_database_url_invalid"),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-safe-pooler.us-east-2.aws.neon.tech/"
            "cutover_ingress?sslmode=require",
            "cutover_ingress_migration_database_must_be_direct",
        ),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-safe.us-east-2.aws.neon.tech/cutover_ingress",
            "cutover_ingress_migration_database_tls_required",
        ),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-other.us-east-2.aws.neon.tech/"
            "cutover_ingress?sslmode=require",
            "cutover_ingress_runtime_migration_database_mismatch",
        ),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-safe.us-east-2.aws.neon.tech/"
            "other_database?sslmode=require",
            "cutover_ingress_runtime_migration_database_mismatch",
        ),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-safe.us-east-2.aws.neon.tech:5433/"
            "cutover_ingress?sslmode=require",
            "cutover_ingress_runtime_migration_database_mismatch",
        ),
        (
            "postgresql+psycopg://migration:secret@"
            "ep-safe.us-east-2.aws.neon.tech/"
            "cutover_ingress?sslmode=require&options=-csearch_path%3Dother",
            "cutover_ingress_database_target_options_forbidden",
        ),
    ],
)
def test_migration_rejects_unsafe_or_mismatched_connection(
    migration_url: str,
    error_code: str,
) -> None:
    with pytest.raises(CutoverIngressConfigurationError, match=error_code):
        _direct_migration_url(
            RUNTIME_POOLED_URL,
            {"CUTOVER_INGRESS_MIGRATIONS_DATABASE_URL": migration_url},
        )
