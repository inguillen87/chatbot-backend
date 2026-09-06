"""Fast Gunicorn entrypoint for Vercel container cold starts.

The application imports a large, intentionally modular Flask route tree.  A
Gunicorn worker that imports that tree during process boot can miss Vercel's
container-initialisation deadline even though the application is healthy.  This
WSGI proxy lets the worker start accepting requests immediately and performs
the one-time application import in the background.  A bounded waiter can use
the application as soon as it is ready; concurrent requests are shed with a
short, retryable ``503`` instead of all occupying Gunicorn threads until
Vercel kills the cold container.

Render keeps using ``app:app``.  This entrypoint is deliberately scoped to the
Vercel Dockerfile so it does not change the established Render runtime.
"""

from __future__ import annotations

import json
import logging
import os
import time
from importlib import import_module
from threading import Event, Lock, Thread
from typing import Any, Callable


StartResponse = Callable[[str, list[tuple[str, str]]], Any]
WsgiApplication = Callable[[dict[str, Any], StartResponse], Any]


logger = logging.getLogger("chatboc.bootstrap")
_TRUTHY_VALUES = frozenset({"1", "true", "t", "yes", "y", "on"})
# Vercel's container readiness budget includes roughly two seconds before the
# first WSGI call reaches this proxy.  Keeping the request-side wait at one
# second leaves enough headroom to return the retryable bootstrap contract
# before the platform terminates an otherwise healthy warming container.
_DEFAULT_STARTUP_WAIT_SECONDS = 1.0
_MAX_STARTUP_WAIT_SECONDS = 1.5


def _is_vercel_runtime() -> bool:
    vercel_env = str(os.getenv("VERCEL_ENV") or "").strip().lower()
    return (
        str(os.getenv("VERCEL") or "").strip().lower() in _TRUTHY_VALUES
        or vercel_env in {"preview", "production"}
        or bool(str(os.getenv("VERCEL_URL") or "").strip())
    )


def _startup_wait_seconds() -> float:
    """Return a bounded wait below Vercel's container-init failure window."""

    raw_value = os.getenv("VERCEL_WSGI_STARTUP_WAIT_SECONDS")
    if raw_value is None:
        return _DEFAULT_STARTUP_WAIT_SECONDS
    try:
        parsed = float(raw_value.strip())
    except (AttributeError, ValueError):
        return _DEFAULT_STARTUP_WAIT_SECONDS
    return min(_MAX_STARTUP_WAIT_SECONDS, max(0.0, parsed))


class LazyApplication:
    """Load the canonical Flask application once, safely across threads.

    ``background_warmup`` is reserved for the Vercel web entrypoint.  Other
    runtimes and tests keep the original synchronous lazy-loading behaviour.
    While that warmup is running, at most one request waits for a bounded
    interval.  Extra concurrent requests receive a small retryable response,
    which prevents a burst of public reads from pinning every Gunicorn thread
    behind the same import lock.
    """

    def __init__(
        self,
        loader: Callable[[], WsgiApplication] | None = None,
        *,
        background_warmup: bool = False,
        startup_wait_seconds: float = _DEFAULT_STARTUP_WAIT_SECONDS,
    ) -> None:
        self._loader = loader or self._load_canonical_application
        self._application: WsgiApplication | None = None
        self._load_failed = False
        self._lock = Lock()
        self._start_lock = Lock()
        self._waiter_lock = Lock()
        self._ready = Event()
        self._warmup_started = False
        self._background_warmup = bool(background_warmup)
        self._startup_wait_seconds = min(
            _MAX_STARTUP_WAIT_SECONDS,
            max(0.0, float(startup_wait_seconds)),
        )
        if self._background_warmup:
            self.start_warmup()

    @staticmethod
    def _load_canonical_application() -> WsgiApplication:
        module = import_module("app")
        flask_application = getattr(module, "app", None)
        if flask_application is None:
            flask_application = module.create_app()
        return flask_application

    def _load_and_cache(self) -> WsgiApplication:
        application = self._application
        if application is not None:
            return application

        with self._lock:
            application = self._application
            if application is None:
                application = self._loader()
                if not callable(application):
                    raise TypeError("The canonical WSGI application is not callable")
                self._application = application
        return application

    def _background_load(self) -> None:
        started_at = time.monotonic()
        try:
            self._load_and_cache()
        except Exception as exc:  # pragma: no cover - exact provider/config error varies
            self._load_failed = True
            # Do not include the exception message: provider and database errors
            # can contain connection strings or other runtime credentials.
            logger.error(
                "Canonical application background initialization failed error_type=%s",
                type(exc).__name__,
            )
        finally:
            self._ready.set()
            logger.info(
                "Canonical application initialization finished ready=%s duration_ms=%d",
                self._application is not None,
                round((time.monotonic() - started_at) * 1000),
            )

    def start_warmup(self) -> None:
        """Start one daemon warmup without delaying Gunicorn socket readiness."""

        with self._start_lock:
            if self._warmup_started:
                return
            self._warmup_started = True
            Thread(
                target=self._background_load,
                name="chatboc-wsgi-warmup",
                daemon=True,
            ).start()

    @staticmethod
    def _bootstrap_response(
        environ: dict[str, Any],
        start_response: StartResponse,
        *,
        failed: bool,
    ) -> list[bytes]:
        reason_code = (
            "application_initialization_failed"
            if failed
            else "application_initializing"
        )
        payload = json.dumps(
            {
                "contract_version": "chatboc.bootstrap.v1",
                "ok": False,
                "status_code": 503,
                "reason_code": reason_code,
                "retryable": not failed,
                "action_hint": "retry_after" if not failed else "inspect_runtime_logs",
                "error": {
                    "code": 503,
                    "message": (
                        "La aplicacion se esta iniciando. Intentalo nuevamente."
                        if not failed
                        else "La aplicacion no pudo inicializarse."
                    ),
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(payload))),
            ("Cache-Control", "no-store"),
            ("Retry-After", "1"),
            ("X-Chatboc-Bootstrap", "failed" if failed else "initializing"),
        ]
        if environ.get("HTTP_ORIGIN"):
            headers.extend(
                [
                    ("Access-Control-Allow-Origin", "*"),
                    ("Vary", "Origin"),
                ]
            )
        start_response("503 Service Unavailable", headers)
        return [payload]

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Any:
        application = self._application
        if application is not None:
            return application(environ, start_response)

        if not self._background_warmup:
            return self._load_and_cache()(environ, start_response)

        self.start_warmup()
        if not self._ready.is_set() and self._waiter_lock.acquire(blocking=False):
            try:
                self._ready.wait(self._startup_wait_seconds)
            finally:
                self._waiter_lock.release()

        application = self._application
        if application is not None:
            return application(environ, start_response)
        return self._bootstrap_response(
            environ,
            start_response,
            failed=self._ready.is_set() and self._load_failed,
        )


application = LazyApplication(
    background_warmup=_is_vercel_runtime(),
    startup_wait_seconds=_startup_wait_seconds(),
)
