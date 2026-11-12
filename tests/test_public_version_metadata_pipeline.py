"""Run the real version blueprint and exact tenant hook with inert ORM seams.

No app factory, configuration module, model, database or provider is imported.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from flask import Flask, current_app, g, request


ROOT = Path(__file__).resolve().parents[1]


def _load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_functions(path, names, namespace):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == names
    body = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *functions]
    exec(compile(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[])), str(path), "exec"), namespace)


class PublicVersionMetadataPipelineTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(BACKEND_VERSION="synthetic-backend", FRONTEND_VERSION="synthetic-frontend",
                               BACKEND_URL="https://backend.example.test", PANEL_URL="https://panel.example.test")
        self.lookups = []
        self.state = None
        self.resolver_calls = 0
        lifecycle = ModuleType("services.auth_session_lifecycle")
        lifecycle.is_retirement_request = lambda: False
        resolver = ModuleType("services.tenant_resolver")
        resolver.apply_tenant_alias = lambda value: value
        self.modules = patch.dict(sys.modules, {
            "services.auth_session_lifecycle": lifecycle,
            "services.tenant_resolver": resolver,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

        metadata = _load_file("isolated_runtime_metadata", ROOT / "utils/runtime_metadata.py")
        liveness = _load_file("isolated_runtime_liveness", ROOT / "utils/runtime_liveness.py")
        namespace = dict(g=g, request=request, current_app=current_app, urlparse=urlparse,
                         _TENANT_HINT_BODY_MAX_BYTES=65_536,
                         is_public_liveness_request=liveness.is_public_liveness_request,
                         is_public_version_metadata_request=metadata.is_public_version_metadata_request)
        names = {"_normalize_slug", "_tenant_slug_from_body", "_tenant_slug_from_url",
                 "_tenant_slug_from_path", "_resolve_tenant_profile", "tenant_middleware"}
        _source_functions(ROOT / "middleware/tenant_context.py", names, namespace)

        def find_tenant(slug):
            self.lookups.append(slug)
            return None
        namespace["_find_tenant_by_slug"] = find_tenant
        resolve = namespace["_resolve_tenant_profile"]
        def record_resolver():
            self.resolver_calls += 1
            return resolve()
        namespace["_resolve_tenant_profile"] = record_resolver

        @self.app.before_request
        def stale_aliases():
            for key in ("tenant_profile", "tenant_profile_slug", "current_tenant",
                        "current_tenant_slug", "tenant_slug", "tenant"):
                setattr(g, key, "stale-request")
        namespace["tenant_middleware"](self.app)
        @self.app.after_request
        def capture_state(response):
            self.state = dict(g.__dict__)
            return response
        self.public_routes = _load_file("isolated_public_config", ROOT / "routes/config.py")
        self.app.register_blueprint(self.public_routes.config_bp)
        self.app.add_url_rule("/api/version/private", "private_version", lambda: {"private": True})

    def _assert_cleared(self):
        for key in ("tenant_profile", "tenant_profile_slug", "current_tenant",
                    "current_tenant_slug", "tenant_slug", "tenant"):
            self.assertNotIn(key, self.state)

    def test_get_and_head_return_real_public_version_without_tenant_resolution(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self.lookups.clear()
                self.resolver_calls = 0
                response = self.app.test_client().open("/api/version", method=method)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.resolver_calls, 0, self.lookups)
                self.assertEqual(self.lookups, [])
                self._assert_cleared()
                if method == "GET":
                    self.assertEqual(response.get_json(), {"backend": "synthetic-backend", "frontend": "synthetic-frontend"})
                else:
                    self.assertEqual(response.data, b"")

    def test_public_version_remains_independent_of_tenant_hints(self):
        response = self.app.test_client().get("/api/version?tenant_slug=unknown", headers={"X-Tenant-Slug": "other"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"backend": "synthetic-backend", "frontend": "synthetic-frontend"})
        self.assertEqual(self.lookups, [])
        self.assertEqual(self.resolver_calls, 0)
        self._assert_cleared()

    def test_neighbors_and_non_version_methods_keep_resolution(self):
        for path, method, slug in (("/api/version/private", "GET", "version"),
                                  ("/api/config", "GET", "config"),
                                  ("/api/version", "OPTIONS", "version"),
                                  ("/api/version", "POST", "version")):
            with self.subTest(path=path, method=method):
                self.lookups.clear()
                self.resolver_calls = 0
                response = self.app.test_client().open(path, method=method)
                self.assertEqual(response.status_code, 405 if method == "POST" else 200)
                self.assertEqual(self.resolver_calls, 1)
                self.assertEqual(self.lookups, [slug])

    def test_same_path_with_a_different_endpoint_keeps_resolution(self):
        with self.app.test_request_context("/api/version", method="GET"):
            request.url_rule = SimpleNamespace(endpoint="private_version")
            self.app.preprocess_request()
        self.assertEqual(self.resolver_calls, 1)
        self.assertEqual(self.lookups, ["version"])


if __name__ == "__main__":
    unittest.main()
