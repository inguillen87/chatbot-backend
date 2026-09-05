"""Fast Gunicorn entrypoint for Vercel container cold starts.

The application imports a large, intentionally modular Flask route tree.  A
Gunicorn worker that imports that tree during process boot can miss Vercel's
container-initialisation deadline even though the application is healthy.  This
WSGI proxy lets the worker start accepting requests immediately and performs
the one-time application import inside the first request, where the regular
request timeout applies.

Render keeps using ``app:app``.  This entrypoint is deliberately scoped to the
Vercel Dockerfile so it does not change the established Render runtime.
"""

from __future__ import annotations

from importlib import import_module
from threading import Lock
from typing import Any, Callable


StartResponse = Callable[[str, list[tuple[str, str]]], Any]
WsgiApplication = Callable[[dict[str, Any], StartResponse], Any]


class LazyApplication:
    """Load the canonical Flask application once, safely across threads."""

    def __init__(self, loader: Callable[[], WsgiApplication] | None = None) -> None:
        self._loader = loader or self._load_canonical_application
        self._application: WsgiApplication | None = None
        self._lock = Lock()

    @staticmethod
    def _load_canonical_application() -> WsgiApplication:
        module = import_module("app")
        flask_application = getattr(module, "app", None)
        if flask_application is None:
            flask_application = module.create_app()
        return flask_application

    def _get_application(self) -> WsgiApplication:
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

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Any:
        return self._get_application()(environ, start_response)


application = LazyApplication()

