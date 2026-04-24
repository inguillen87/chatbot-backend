import unittest
from types import SimpleNamespace

from flask import Flask, g
from werkzeug.exceptions import HTTPException

from services.analytics.rbac import require_access


class AnalyticsRbacCapabilitiesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["TESTING"] = False

    def test_allows_when_capability_present(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=1,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={"permisos": ["analytics.read", "analytics.admin"]},
            )
            viewer = require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(viewer.role, "operador")

    def test_denies_when_capability_missing(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=2,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={"permisos": ["analytics.read"]},
            )
            with self.assertRaises(HTTPException) as ctx:
                require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(ctx.exception.code, 403)

    def test_legacy_fallback_when_capabilities_absent(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=3,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={},
            )
            viewer = require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(viewer.role, "operador")

    def test_reads_capabilities_from_employee_scope_in_accesibilidad(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=4,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={},
                accesibilidad={"employee_scope": {"permisos": ["analytics.read"]}},
            )
            with self.assertRaises(HTTPException) as ctx:
                require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(ctx.exception.code, 403)

    def test_allows_when_capability_wildcard_present(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=5,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={"permisos": ["*"]},
            )
            viewer = require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(viewer.role, "operador")

    def test_reads_capabilities_from_comma_separated_permissions_string(self):
        with self.app.test_request_context("/"):
            g.viewer = SimpleNamespace(
                id=6,
                rol="operador",
                municipio_id=10,
                pyme_id=None,
                empresa_id=None,
                scope={"permissions": "analytics.read, analytics.admin"},
            )
            viewer = require_access("10", "operador", required_capability="analytics.admin")
            self.assertEqual(viewer.role, "operador")


if __name__ == "__main__":
    unittest.main()
