from __future__ import annotations

import pytest

from config import build_database_engine_options, resolve_database_uri
from utils.runtime_environment import (
    is_production_runtime,
    is_vercel_runtime,
    resolved_runtime_environment,
)


def test_vercel_preview_is_production_like() -> None:
    environ = {"VERCEL": "1", "VERCEL_ENV": "preview"}

    assert is_vercel_runtime(environ)
    assert is_production_runtime(environ=environ)
    assert resolved_runtime_environment(environ=environ) == "prod"


def test_vercel_runtime_requires_external_database() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL es obligatoria"):
        resolve_database_uri(environ={"VERCEL": "1", "VERCEL_ENV": "preview"})


def test_vercel_runtime_accepts_external_database() -> None:
    uri = "postgresql://user:password@example.invalid:5432/chatboc"

    assert resolve_database_uri(
        environ={
            "VERCEL": "1",
            "VERCEL_ENV": "preview",
            "DATABASE_URL": uri,
        }
    ) == uri


def test_render_legacy_mode_preserves_existing_sqlite_fallback() -> None:
    assert resolve_database_uri(
        environ={
            "RENDER": "true",
            "CHATBOC_RENDER_STANDBY_MODE": "false",
        }
    ) == "sqlite:////data/database.db?check_same_thread=False"


@pytest.mark.parametrize("flag_value", ["true", "typo", ""])
def test_render_standby_mode_fails_closed_without_neon_database(flag_value: str) -> None:
    with pytest.raises(RuntimeError, match="Neon es obligatoria"):
        resolve_database_uri(
            environ={
                "RENDER": "true",
                "CHATBOC_RENDER_STANDBY_MODE": flag_value,
            }
        )


@pytest.mark.parametrize(
    "uri",
    [
        "sqlite:////data/database.db",
        "postgresql://user:password@example.invalid/chatboc",
    ],
)
def test_render_standby_mode_rejects_non_neon_database(uri: str) -> None:
    with pytest.raises(RuntimeError, match="PostgreSQL en Neon"):
        resolve_database_uri(
            environ={
                "RENDER": "true",
                "CHATBOC_RENDER_STANDBY_MODE": "true",
                "DATABASE_URL": uri,
            }
        )


def test_render_standby_mode_accepts_neon_runtime_database() -> None:
    uri = "postgresql://user:password@ep-example-pooler.us-east-2.aws.neon.tech/chatboc?sslmode=require"

    assert resolve_database_uri(
        environ={
            "RENDER": "true",
            "CHATBOC_RENDER_STANDBY_MODE": "true",
            "DATABASE_URL": uri,
        }
    ) == uri


def test_database_pool_is_bounded_and_configurable() -> None:
    options = build_database_engine_options(
        "postgresql://user:password@example.invalid:5432/chatboc",
        pool_size="3",
        max_overflow="2",
    )

    assert options["pool_size"] == 3
    assert options["max_overflow"] == 2

    bounded = build_database_engine_options(
        "postgresql://user:password@example.invalid:5432/chatboc",
        pool_size="500",
        max_overflow="500",
    )
    assert bounded["pool_size"] == 50
    assert bounded["max_overflow"] == 50
