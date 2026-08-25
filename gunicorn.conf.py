# gunicorn.conf.py
# Timeout setting for Gunicorn workers
# This prevents the server from killing long-running requests,
# such as those waiting for a response from an external LLM API.
#
# Allow overriding via environment variables. The web runtime deliberately
# remains a single gthread worker because Socket.IO room affinity is not safe
# across multiple workers in one instance. Thread capacity is bounded so an
# invalid deployment override cannot silently collapse or exhaust the process.
import os


DEFAULT_GUNICORN_THREADS = 32
MIN_GUNICORN_THREADS = 16
MAX_GUNICORN_THREADS = 64


def _parse_integer_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value.strip())
    except (AttributeError, ValueError):
        raise RuntimeError(f"{name} must be an integer") from None


timeout = _parse_integer_env("GUNICORN_TIMEOUT", 300)
worker_class = "gthread"
configured_workers = _parse_integer_env("GUNICORN_WORKERS", 1)
if configured_workers != 1:
    raise RuntimeError(
        "GUNICORN_WORKERS must be 1; scale single-worker instances behind sticky sessions"
    )
workers = 1
configured_threads = _parse_integer_env(
    "GUNICORN_THREADS", DEFAULT_GUNICORN_THREADS
)
threads = min(
    MAX_GUNICORN_THREADS,
    max(MIN_GUNICORN_THREADS, configured_threads),
)
