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
graceful_timeout = _parse_integer_env(
    "GUNICORN_GRACEFUL_TIMEOUT",
    min(timeout, 285),
)
if graceful_timeout <= 0:
    raise RuntimeError("GUNICORN_GRACEFUL_TIMEOUT must be greater than zero")
if graceful_timeout > timeout:
    raise RuntimeError("GUNICORN_GRACEFUL_TIMEOUT must not exceed GUNICORN_TIMEOUT")
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


def post_worker_init(worker):
    """Schedule Vercel warmup after WSGI setup, never in the master process.

    start_warmup schedules the existing delayed single-flight loader and
    returns immediately. No request is dispatched and Render is unchanged.
    Waiting for the first incoming request unnecessarily spends its latency
    budget before even starting the canonical application's imports.
    """
    from bootstrap_wsgi import _is_vercel_runtime

    if not _is_vercel_runtime():
        return
    warmup = getattr(worker.wsgi, "start_warmup", None)
    if callable(warmup):
        warmup()
