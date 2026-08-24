# gunicorn.conf.py
# Timeout setting for Gunicorn workers
# This prevents the server from killing long-running requests,
# such as those waiting for a response from an external LLM API.
#
# Allow overriding via environment variable `GUNICORN_TIMEOUT`.
import os

timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
worker_class = "gthread"
configured_workers = int(os.getenv("GUNICORN_WORKERS", "1"))
if configured_workers != 1:
    raise RuntimeError(
        "GUNICORN_WORKERS must be 1; scale single-worker instances behind sticky sessions"
    )
workers = 1
threads = int(os.getenv("GUNICORN_THREADS", "100"))
