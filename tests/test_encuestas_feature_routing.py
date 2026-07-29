from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROUTE_MATRIX_MARKER = "SURVEY_ROUTE_MATRIX="

EXPECTED_CANONICAL_ROUTES = {
    "/api/public/encuestas": (
        "encuestas_public_bp.listar_publicas",
        "routes.encuestas_public",
    ),
    "/api/public/encuestas/<slug>": (
        "encuestas_public_bp.obtener_encuesta",
        "routes.encuestas_public",
    ),
    "/api/public/encuestas/<slug>/respuestas": (
        "encuestas_public_bp.responder_alias",
        "routes.encuestas_public",
    ),
    "/public/encuestas": (
        "encuestas_public_legacy_bp.listar_publicas",
        "routes.encuestas_public",
    ),
    "/public/encuestas/<slug>": (
        "encuestas_public_legacy_bp.obtener_encuesta",
        "routes.encuestas_public",
    ),
    "/public/encuestas/<slug>/respuestas": (
        "encuestas_public_legacy_bp.responder_alias",
        "routes.encuestas_public",
    ),
    "/e/<slug>": (
        "encuestas_public_share_bp.share_redirect",
        "routes.encuestas_public",
    ),
}


def _survey_route_matrix(*, feature_enabled: bool) -> dict:
    target_routes = sorted(EXPECTED_CANONICAL_ROUTES)
    probe = textwrap.dedent(
        f"""
        import json
        import sys

        from app import create_app
        from config import TestingConfig

        app = create_app(TestingConfig)
        target_routes = {target_routes!r}
        routes = {{}}
        for rule in app.url_map.iter_rules():
            path = str(rule)
            if path not in target_routes:
                continue
            view = app.view_functions[rule.endpoint]
            routes[path] = {{
                "endpoint": rule.endpoint,
                "module": view.__module__,
            }}

        legacy_view_modules = sorted({{
            view.__module__
            for view in app.view_functions.values()
            if view.__module__.startswith("routes.encuestas_publicas")
        }})
        print(
            {ROUTE_MATRIX_MARKER!r}
            + json.dumps(
                {{
                    "feature_enabled": {feature_enabled!r},
                    "routes": routes,
                    "legacy_view_modules": legacy_view_modules,
                    "legacy_module_loaded": "routes.encuestas_publicas" in sys.modules,
                }},
                sort_keys=True,
            )
        )
        """
    )

    environment = os.environ.copy()
    environment.update(
        {
            "FEATURE_ENCUESTAS": "1" if feature_enabled else "0",
            "FLASK_MIGRATIONS_ONLY": "0",
            "FLASK_SKIP_GLOBAL_APP": "1",
            "FLASK_ENV": "testing",
            "TESTING": "1",
            "DATABASE_URL": "sqlite:///:memory:",
            "ENABLE_RUNTIME_SCHEMA_SYNC": "0",
            "ENABLE_RUNTIME_TENANT_INIT": "0",
            "SKIP_INIT_TENANTS": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    marker_line = next(
        (
            line
            for line in completed.stdout.splitlines()
            if line.startswith(ROUTE_MATRIX_MARKER)
        ),
        None,
    )
    assert marker_line is not None, completed.stdout + completed.stderr
    return json.loads(marker_line.removeprefix(ROUTE_MATRIX_MARKER))


@pytest.mark.parametrize("feature_enabled", [False, True])
def test_public_survey_routing_never_mounts_retired_stack(feature_enabled: bool):
    matrix = _survey_route_matrix(feature_enabled=feature_enabled)

    assert matrix["feature_enabled"] is feature_enabled
    assert matrix["legacy_view_modules"] == []
    assert matrix["legacy_module_loaded"] is False
    assert matrix["routes"] == {
        path: {"endpoint": endpoint, "module": module}
        for path, (endpoint, module) in EXPECTED_CANONICAL_ROUTES.items()
    }
