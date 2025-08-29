# gunicorn.conf.py
# Timeout setting for Gunicorn workers
# This prevents the server from killing long-running requests,
# such as those waiting for a response from the Gemini API.
#
# Allow overriding via environment variable `GUNICORN_TIMEOUT`.
import os

timeout = int(os.getenv("GUNICORN_TIMEOUT", "300"))
worker_class = 'eventlet'
