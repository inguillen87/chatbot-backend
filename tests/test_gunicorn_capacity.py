from __future__ import annotations

import os
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
GUNICORN_CONFIG = REPO_ROOT / "gunicorn.conf.py"


def _load_config(**overrides: str) -> dict[str, object]:
    environment = {
        "GUNICORN_WORKERS": "1",
        "GUNICORN_TIMEOUT": "300",
        **overrides,
    }
    with patch.dict(os.environ, environment, clear=True):
        return runpy.run_path(str(GUNICORN_CONFIG))


def test_vercel_image_defaults_to_single_worker_and_32_threads() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.vercel").read_text(encoding="utf-8")

    assert "GUNICORN_WORKERS=1" in dockerfile
    assert "GUNICORN_THREADS=32" in dockerfile


def test_vercel_image_precompiles_application_after_copy() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile.vercel").read_text(encoding="utf-8")

    copy_index = dockerfile.index("COPY . .")
    compile_index = dockerfile.index("RUN python -m compileall -q -j 0 /app")
    command_index = dockerfile.index('CMD ["sh"')

    assert copy_index < compile_index < command_index


def test_gunicorn_default_has_safe_minimum_socket_capacity() -> None:
    config = _load_config()

    assert config["worker_class"] == "gthread"
    assert config["workers"] == 1
    assert config["threads"] == 100
    assert config["graceful_timeout"] == 285


def test_vercel_runtime_defaults_to_bounded_32_thread_capacity() -> None:
    config = _load_config(VERCEL="1")

    assert config["threads"] == 32
    assert config["threads"] >= config["VERCEL_MIN_GUNICORN_THREADS"]


@pytest.mark.parametrize("configured", [16, 24, 32, 48, 64])
def test_gunicorn_preserves_explicit_thread_overrides_in_safe_range(
    configured: int,
) -> None:
    config = _load_config(VERCEL="1", GUNICORN_THREADS=str(configured))

    assert config["threads"] == configured


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (0, 16),
        (15, 16),
        (65, 64),
        (100, 64),
    ],
)
def test_gunicorn_clamps_thread_overrides_to_safe_range(
    configured: int,
    expected: int,
) -> None:
    config = _load_config(VERCEL="1", GUNICORN_THREADS=str(configured))

    assert config["threads"] == expected
    assert 16 <= config["threads"] <= 64


@pytest.mark.parametrize("configured", [8, 32, 100, 128])
def test_non_vercel_runtime_preserves_existing_thread_overrides(configured: int) -> None:
    config = _load_config(GUNICORN_THREADS=str(configured))

    assert config["threads"] == configured


@pytest.mark.parametrize("invalid", ["", "many", "32.5", "0x20"])
def test_gunicorn_rejects_non_integer_thread_overrides(invalid: str) -> None:
    with pytest.raises(RuntimeError, match="GUNICORN_THREADS must be an integer"):
        _load_config(GUNICORN_THREADS=invalid)


def test_gunicorn_rejects_non_integer_worker_override() -> None:
    with pytest.raises(RuntimeError, match="GUNICORN_WORKERS must be an integer"):
        _load_config(GUNICORN_WORKERS="many")


def test_gunicorn_rejects_multiple_workers_in_one_instance() -> None:
    with pytest.raises(RuntimeError, match="GUNICORN_WORKERS must be 1"):
        _load_config(GUNICORN_WORKERS="2")


def test_gunicorn_preserves_explicit_timeout_override() -> None:
    config = _load_config(GUNICORN_TIMEOUT="321")

    assert config["timeout"] == 321


def test_gunicorn_preserves_bounded_graceful_timeout_override() -> None:
    config = _load_config(
        GUNICORN_TIMEOUT="300",
        GUNICORN_GRACEFUL_TIMEOUT="240",
    )

    assert config["graceful_timeout"] == 240


@pytest.mark.parametrize("configured", ["0", "-1"])
def test_gunicorn_rejects_non_positive_graceful_timeout(configured: str) -> None:
    with pytest.raises(RuntimeError, match="must be greater than zero"):
        _load_config(GUNICORN_GRACEFUL_TIMEOUT=configured)


def test_gunicorn_rejects_graceful_timeout_above_hard_timeout() -> None:
    with pytest.raises(RuntimeError, match="must not exceed GUNICORN_TIMEOUT"):
        _load_config(GUNICORN_TIMEOUT="60", GUNICORN_GRACEFUL_TIMEOUT="61")


def test_gunicorn_rejects_non_integer_timeout_override() -> None:
    with pytest.raises(RuntimeError, match="GUNICORN_TIMEOUT must be an integer"):
        _load_config(GUNICORN_TIMEOUT="slow")


def test_gunicorn_rejects_non_integer_graceful_timeout_override() -> None:
    with pytest.raises(RuntimeError, match="GUNICORN_GRACEFUL_TIMEOUT must be an integer"):
        _load_config(GUNICORN_GRACEFUL_TIMEOUT="slow")
