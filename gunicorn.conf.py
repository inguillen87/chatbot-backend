# gunicorn.conf.py
# Timeout setting for Gunicorn workers
# This prevents the server from killing long-running requests,
# such as those waiting for a response from the Gemini API.
timeout = 120
worker_class = 'eventlet'
