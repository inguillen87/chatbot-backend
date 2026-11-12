"""Run the exact request hooks with Flask and isolated persistence seams.

The application factory is intentionally not imported: it loads configuration
and providers unrelated to liveness. Source hooks run unchanged in a minimal
Flask request pipeline; no database or external service is opened.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask, current_app, g, jsonify, request
from flask.sessions import SecureCookieSession, SessionInterface

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils.runtime_liveness import is_public_liveness_request


def _source_function(relative_path, name, namespace):
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8-sig"))
    matches = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1, name
    function = matches[0]
    function.decorator_list = []
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(ROOT / relative_path), "exec"), namespace)
    return namespace[name]


class _LazyActor:
    def __init__(self):
        self.lookups = 0

    def __bool__(self):
        self.lookups += 1
        return True

    @property
    def is_authenticated(self):
        return False


class _RecordingSessionInterface(SessionInterface):
    """Model the pre-hook storage seam, retaining the ambient-cookie boundary."""

    def __init__(self):
        self.opened = []
        self.saved = 0
        self.cookie_storage_attempts = 0

    def open_session(self, app, req):
        cookie = req.cookies.get("session")
        self.opened.append(cookie)
        if cookie:
            self.cookie_storage_attempts += 1
        return SecureCookieSession()

    def save_session(self, app, session, response):
        self.saved += 1


class PublicLivenessPipelineTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "synthetic-unit-test-only"
        self.lazy_actor = _LazyActor()
        self.actor = SimpleNamespace(id=11)
        self.tenant = SimpleNamespace(id=71, slug="synthetic-tenant")
        self.token = Mock(return_value="synthetic-token")
        self.identity = Mock(return_value=self.actor)
        self.contact = Mock(return_value={"contact_key": "synthetic-contact"})
        self.body_allowed = Mock(return_value=False)
        self.tenant_resolver = Mock(return_value=self.tenant)

        auth = ModuleType("utils.auth_helpers")
        auth.obtener_token, auth.user_from_token = self.token, self.identity
        login = ModuleType("flask_login")
        login.current_user = self.lazy_actor
        lifecycle = ModuleType("services.auth_session_lifecycle")
        lifecycle.is_retirement_request = lambda: False
        self.modules = patch.dict(sys.modules, {
            "utils.auth_helpers": auth,
            "flask_login": login,
            "services.auth_session_lifecycle": lifecycle,
        })
        self.modules.start()
        self.addCleanup(self.modules.stop)

        namespace = {
            "g": g, "request": request, "current_app": current_app,
            "jsonify": jsonify, "re": re,
            "is_public_liveness_request": is_public_liveness_request,
            "resolve_contact_identity_from_request": self.contact,
            "request_path_allows_contact_identity_body": self.body_allowed,
            "_resolve_tenant_profile": self.tenant_resolver,
        }
        self.user_hook = _source_function("app.py", "attach_current_user", namespace)
        self.contact_hook = _source_function("app.py", "attach_contact_identity", namespace)
        self.tenant_hook = _source_function("middleware/tenant_context.py", "attach_tenant_profile", namespace)
        health = _source_function("app.py", "health", namespace)
        self.state = None

        @self.app.before_request
        def stale_context():
            for key in (
                "viewer", "_login_user", "current_user", "token_payload",
                "auth_credential_source", "explicit_bearer_present", "contact_identity",
                "tenant_profile", "tenant_profile_slug", "current_tenant",
                "current_tenant_slug", "tenant_slug", "tenant",
            ):
                setattr(g, key, "stale")

        self.app.before_request(self.user_hook)
        self.app.before_request(self.contact_hook)
        self.app.before_request(self.tenant_hook)

        @self.app.before_request
        def capture_context():
            self.state = dict(g.__dict__)

        self.app.add_url_rule("/health", endpoint="health", view_func=health, methods=["GET", "HEAD"])
        for index, path in enumerate(("/health/ready", "/api/version", "/auth/me", "/health/", "/health/private")):
            self.app.add_url_rule(path, endpoint=f"neighbor_{index}", view_func=lambda: jsonify({"ok": True}))

    def _clear_counts(self):
        for seam in (self.token, self.identity, self.contact, self.body_allowed, self.tenant_resolver):
            seam.reset_mock()
        self.lazy_actor.lookups = 0

    def _assert_no_resolution(self):
        for seam in (self.token, self.identity, self.contact, self.body_allowed, self.tenant_resolver):
            seam.assert_not_called()
        self.assertEqual(self.lazy_actor.lookups, 0)

    def _assert_cleared_context(self):
        for key in ("viewer", "current_user", "auth_credential_source", "contact_identity"):
            self.assertIsNone(self.state[key], key)
        self.assertNotIn("_login_user", self.state)
        self.assertEqual(self.state["token_payload"], {})
        self.assertFalse(self.state["explicit_bearer_present"])
        for key in ("tenant_profile", "tenant_profile_slug", "current_tenant", "current_tenant_slug", "tenant_slug", "tenant"):
            self.assertNotIn(key, self.state)

    def test_anonymous_get_and_head_do_not_resolve_identity_contact_or_tenant(self):
        client = self.app.test_client()
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self._clear_counts()
                response = client.open("/health", method=method)
                self.assertEqual(response.status_code, 200)
                if method == "GET":
                    self.assertEqual(response.get_json(), {"status": "ok"})
                else:
                    self.assertEqual(response.data, b"")
                self._assert_no_resolution()
                self._assert_cleared_context()

    def test_liveness_ignores_bearer_contact_tenant_and_body_hints(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                self._clear_counts()
                with patch("flask.wrappers.Request.get_json", side_effect=AssertionError("Body must not be materialized")):
                    response = self.app.test_client().open(
                        "/health?tenant_slug=untrusted", method=method, data='{"tenant":"untrusted"}',
                        content_type="application/json", headers={
                            "Authorization": "Bearer untrusted",
                            "X-Tenant-Slug": "untrusted", "X-Contact-Key": "untrusted",
                            "X-Anon-Id": "untrusted", "Origin": "https://example.invalid",
                        },
                    )
                self.assertEqual(response.status_code, 200)
                self._assert_no_resolution()
                self._assert_cleared_context()

    def test_neighboring_routes_and_non_liveness_methods_retain_normal_hooks(self):
        for path, method in (
            ("/health/ready", "GET"), ("/api/version", "GET"), ("/auth/me", "GET"),
            ("/health/", "GET"), ("/health/private", "GET"),
            ("/health", "OPTIONS"), ("/health", "POST"),
        ):
            with self.subTest(path=path, method=method):
                self._clear_counts()
                self.app.test_client().open(path, method=method)
                self.token.assert_called_once_with()
                self.identity.assert_called_once_with("synthetic-token")
                self.contact.assert_called_once()
                self.body_allowed.assert_called_once_with(path)
                self.tenant_resolver.assert_called_once_with()
                self.assertEqual(self.lazy_actor.lookups, 1)
                self.assertIs(self.state["viewer"], self.actor)
                self.assertEqual(self.state["contact_identity"], {"contact_key": "synthetic-contact"})
                self.assertIs(self.state["tenant_profile"], self.tenant)
                self.assertEqual(self.state["current_tenant_slug"], self.tenant.slug)

    def test_an_alternate_endpoint_at_the_same_path_retains_normal_hooks(self):
        with self.app.test_request_context("/health", method="GET"):
            request.url_rule = SimpleNamespace(endpoint="private_health")
            self.user_hook()
            self.contact_hook()
            self.tenant_hook()
        self.identity.assert_called_once_with("synthetic-token")
        self.contact.assert_called_once()
        self.tenant_resolver.assert_called_once_with()

    def test_ambient_session_storage_lifecycle_is_not_exempted(self):
        interface = _RecordingSessionInterface()
        self.app.session_interface = interface
        client = self.app.test_client()
        client.set_cookie("session", "synthetic-legacy-sid")
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(interface.opened, ["synthetic-legacy-sid"])
        self.assertEqual(interface.cookie_storage_attempts, 1)
        self.assertEqual(interface.saved, 1)
        self._assert_no_resolution()


if __name__ == "__main__":
    unittest.main()
