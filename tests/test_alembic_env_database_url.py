from __future__ import annotations

import contextlib
import importlib.util
import sys
import types
from pathlib import Path

from alembic.config import Config
from sqlalchemy.engine import make_url


ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "migrations" / "env.py"


class _OfflineAlembicContext:
    """Minimal Alembic context that cannot open a database connection."""

    def __init__(self, raw_url: str):
        self.config = Config()
        self._raw_url = raw_url
        self.configure_calls: list[dict[str, object]] = []
        self.migrations_run = 0

    def get_x_argument(self, *, as_dictionary: bool = False):
        assert as_dictionary is True
        return {"dburl": self._raw_url}

    def is_offline_mode(self) -> bool:
        return True

    def configure(self, **kwargs) -> None:
        self.configure_calls.append(kwargs)

    def begin_transaction(self):
        return contextlib.nullcontext()

    def run_migrations(self) -> None:
        self.migrations_run += 1


def _load_env_module(monkeypatch, raw_url: str):
    fake_context = _OfflineAlembicContext(raw_url)

    fake_alembic = types.ModuleType("alembic")
    fake_alembic.context = fake_context
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    fake_extensions = types.ModuleType("extensions")
    fake_extensions.db = types.SimpleNamespace(metadata=object())
    monkeypatch.setitem(sys.modules, "extensions", fake_extensions)

    for module_name in (
        "models",
        "models_memory",
        "models_interviews",
        "models_survey_governance",
        "models_survey_eligibility",
        "models_voice_lifecycle",
        "models_whatsapp_workflows",
    ):
        monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))

    module_name = "_test_migrations_env"
    spec = importlib.util.spec_from_file_location(module_name, ENV_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module, fake_context


def test_normalize_keeps_percent_encoded_password_when_switching_driver(
    monkeypatch, capsys
):
    raw_url = (
        "postgresql://migration_user:p%40ss%3Aword%2F100%25@"
        "db.example.test/app?application_name=alembic"
    )

    env, fake_context = _load_env_module(monkeypatch, raw_url)
    stdout = capsys.readouterr().out

    assert make_url(env.DB_URL).drivername == "postgresql+psycopg"
    assert make_url(env.DB_URL).password == "p@ss:word/100%"
    assert "p%40ss%3Aword%2F100%25" in env.DB_URL
    assert "***" not in env.DB_URL

    # ConfigParser receives escaped percent signs but exposes the same usable
    # URL back to Alembic. The offline migration context also gets that URL.
    assert env.config.get_main_option("sqlalchemy.url") == env.DB_URL
    assert fake_context.configure_calls[0]["url"] == env.DB_URL
    assert fake_context.migrations_run == 1

    assert "p@ss:word/100%" not in stdout
    assert "p%40ss%3Aword%2F100%25" not in stdout
    assert "migration_user" not in stdout
    assert "application_name" not in stdout
    assert "postgresql+psycopg://db.example.test/app" in stdout


def test_normalize_adds_render_tls_without_losing_encoded_credentials(
    monkeypatch, capsys
):
    raw_url = (
        "postgresql://render_user:s%40fe%2Fpass%25@service.render.com/app"
        "?application_name=migrations"
    )

    env, _ = _load_env_module(monkeypatch, raw_url)
    stdout = capsys.readouterr().out
    normalized = make_url(env.DB_URL)

    assert normalized.drivername == "postgresql+psycopg"
    assert normalized.password == "s@fe/pass%"
    assert normalized.query["application_name"] == "migrations"
    assert normalized.query["sslmode"] == "require"
    assert "s%40fe%2Fpass%25" in env.DB_URL
    assert "s@fe/pass%" not in stdout
    assert "s%40fe%2Fpass%25" not in stdout


def test_mask_fails_closed_for_valid_and_malformed_values(monkeypatch, capsys):
    env, _ = _load_env_module(
        monkeypatch,
        "postgresql://user:encoded%40secret@db.example.test/app",
    )
    capsys.readouterr()

    masked = env._mask(
        "postgresql+psycopg://user:encoded%40secret@db.example.test/app"
    )

    assert masked == "postgresql+psycopg://db.example.test/app"
    assert "user" not in masked
    assert "encoded@secret" not in masked
    assert "encoded%40secret" not in masked
    assert env._mask("not a database URL containing clear-secret") == (
        "<invalid database URL>"
    )
