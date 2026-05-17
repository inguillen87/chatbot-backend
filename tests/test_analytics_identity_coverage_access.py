import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from routes.analytics import analytics_bp, _resolve_identity_event_tenant_id


class _FakeQuery:
    def filter(self, *_args, **_kwargs):
        return self

    def with_entities(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _FakeColumn:
    def label(self, _name):
        return self

    def desc(self):
        return self


class _FakeAnalyticsEventModel:
    tenant_id = _FakeColumn()
    ts = _FakeColumn()
    channel = _FakeColumn()
    metadata_payload = _FakeColumn()
    session_id = _FakeColumn()
    anon_id = _FakeColumn()
    query = _FakeQuery()


class _FailingQuery(_FakeQuery):
    def all(self):
        raise SQLAlchemyError("analytics event store unavailable")


class _FailingAnalyticsEventModel(_FakeAnalyticsEventModel):
    query = _FailingQuery()


class AnalyticsIdentityCoverageAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(analytics_bp)
        self.client = self.app.test_client()
        self.filters = SimpleNamespace(tenant_id="10", date_from=None, date_to=None, scope="municipio")

    def test_identity_coverage_uses_read_capability_for_regular_reads(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None) as mock_require_access, patch(
            "routes.analytics.AnalyticsEventV2", _FakeAnalyticsEventModel
        ), patch(
            "routes.analytics._compute_identity_coverage",
            return_value={"coverage_pct": 100.0, "channels": {}, "total_events": 0, "events_with_identity": 0},
        ):
            response = self.client.get("/analytics/identity/coverage?tenant_id=10")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))
        self.assertEqual(mock_require_access.call_count, 1)
        mock_require_access.assert_called_once_with(
            "10",
            "visor",
            required_capability="analytics.read",
        )

    def test_identity_coverage_emit_alerts_requires_admin_capability(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None) as mock_require_access, patch(
            "routes.analytics.AnalyticsEventV2", _FakeAnalyticsEventModel
        ), patch(
            "routes.analytics._compute_identity_coverage",
            return_value={"coverage_pct": 70.0, "channels": {"web": {"coverage_pct": 70.0}}, "total_events": 10, "events_with_identity": 7},
        ), patch(
            "routes.analytics.analytics_ingestor.track", return_value=None
        ):
            response = self.client.get("/analytics/identity/coverage?tenant_id=10&emit_alert_events=1")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))
        self.assertEqual(mock_require_access.call_count, 2)
        self.assertEqual(
            mock_require_access.call_args_list[0].args,
            ("10", "visor"),
        )
        self.assertEqual(
            mock_require_access.call_args_list[0].kwargs,
            {"required_capability": "analytics.read"},
        )
        self.assertEqual(
            mock_require_access.call_args_list[1].args,
            ("10", "operador"),
        )
        self.assertEqual(
            mock_require_access.call_args_list[1].kwargs,
            {"required_capability": "analytics.admin"},
        )

    def test_identity_coverage_rejects_non_numeric_limit_with_standard_error(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None):
            response = self.client.get("/analytics/identity/coverage?tenant_id=10&limit=abc")

        self.assertEqual(response.status_code, 400)
        body = response.get_json()
        self.assertEqual(body["error"]["code"], 400)
        self.assertIn("limit", body["error"]["message"])
        self.assertTrue(body.get("request_id"))
        self.assertTrue(response.headers.get("X-Request-Id"))

    def test_identity_coverage_preserves_request_id_header(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None), patch(
            "routes.analytics.AnalyticsEventV2", _FakeAnalyticsEventModel
        ), patch(
            "routes.analytics._compute_identity_coverage",
            return_value={"coverage_pct": 100.0, "channels": {}, "total_events": 0, "events_with_identity": 0},
        ):
            response = self.client.get(
                "/analytics/identity/coverage?tenant_id=10",
                headers={"X-Request-Id": "req-coverage-1"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["request_id"], "req-coverage-1")
        self.assertEqual(response.headers.get("X-Request-Id"), "req-coverage-1")

    def test_identity_coverage_resolves_slug_to_event_store_tenant_id(self):
        class _FakeTenant:
            id = 77
            slug = "junin-1"
            municipio_id = 10
            pyme_id = None

        class _FakeTenantQuery:
            def filter(self, *_args, **_kwargs):
                return self

            def first(self):
                return _FakeTenant()

        class _FakeSlug:
            def ilike(self, value):
                return value

        class _FakeTenantProfile:
            slug = _FakeSlug()
            query = _FakeTenantQuery()

        with self.app.test_request_context("/analytics/identity/coverage?tenant_slug=junin-1"):
            with patch("routes.analytics.TenantProfile", _FakeTenantProfile):
                tenant_id, resolution = _resolve_identity_event_tenant_id(self.filters)

        self.assertEqual(tenant_id, 77)
        self.assertEqual(resolution["tenant_slug"], "junin-1")
        self.assertEqual(resolution["owner_tenant_id"], 10)

    def test_identity_coverage_degrades_when_event_query_fails(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None), patch(
            "routes.analytics.AnalyticsEventV2", _FailingAnalyticsEventModel
        ):
            response = self.client.get("/analytics/identity/coverage?tenant_id=10")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data_status"], "degraded")
        self.assertEqual(body["total_events"], 0)
        self.assertEqual(body["warnings"][0]["code"], "identity_coverage_query_failed")

    def test_identity_coverage_degrades_when_alert_emit_fails(self):
        with patch("routes.analytics.get_config", return_value=SimpleNamespace(feature_enabled=True)), patch(
            "routes.analytics.parse_filters", return_value=self.filters
        ), patch("routes.analytics.require_access", return_value=None), patch(
            "routes.analytics.AnalyticsEventV2", _FakeAnalyticsEventModel
        ), patch(
            "routes.analytics._compute_identity_coverage",
            return_value={"coverage_pct": 70.0, "channels": {"web": {"coverage_pct": 70.0}}, "total_events": 10, "events_with_identity": 7},
        ), patch(
            "routes.analytics.analytics_ingestor.track", side_effect=RuntimeError("emit failed")
        ):
            response = self.client.get("/analytics/identity/coverage?tenant_id=10&emit_alert_events=1")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["data_status"], "degraded")
        self.assertEqual(body["alert_events_emitted"], 0)
        self.assertEqual(body["warnings"][0]["code"], "identity_coverage_alert_emit_failed")


if __name__ == "__main__":
    unittest.main()
