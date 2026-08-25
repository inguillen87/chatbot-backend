# gunicorn.conf.py
# Timeout setting for Gunicorn workers
# This prevents the server from killing long-running requests,
# such as those waiting for a response from an external LLM API.
#
# Allow overriding via environment variables. The web runtime deliberately
# remains a single gthread worker because Socket.IO room affinity is not safe
# across multiple workers in one instance. Vercel thread capacity is bounded;
# other runtimes retain their established 100-thread default and overrides.
import os


DEFAULT_GUNICORN_THREADS = 100
VERCEL_DEFAULT_GUNICORN_THREADS = 32
VERCEL_MIN_GUNICORN_THREADS = 16
VERCEL_MAX_GUNICORN_THREADS = 64


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
is_vercel_runtime = bool(os.getenv("VERCEL") or os.getenv("VERCEL_ENV"))
configured_threads = _parse_integer_env(
    "GUNICORN_THREADS",
    VERCEL_DEFAULT_GUNICORN_THREADS if is_vercel_runtime else DEFAULT_GUNICORN_THREADS,
)
threads = (
    min(
        VERCEL_MAX_GUNICORN_THREADS,
        max(VERCEL_MIN_GUNICORN_THREADS, configured_threads),
    )
    if is_vercel_runtime
    else configured_threads
)
