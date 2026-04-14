import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.analytics import analytics_bp


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


if __name__ == "__main__":
    unittest.main()
