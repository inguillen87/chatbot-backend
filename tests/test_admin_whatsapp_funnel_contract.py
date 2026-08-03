import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes.admin_analytics import WHATSAPP_FUNNEL_CONTRACT_VERSION, _build_whatsapp_funnel_payload


class _DummyColumn:
    def __eq__(self, other):
        return ("eq", other)

    def __ge__(self, other):
        return ("ge", other)

    def __le__(self, other):
        return ("le", other)

    def in_(self, values):
        return ("in", tuple(values))


class _DummyQuery:
    def filter(self, *args, **kwargs):
        return self

    def with_entities(self, *args, **kwargs):
        return self

    def all(self):
        return []


class _DummyAnalyticsEventV2:
    query = _DummyQuery()
    tenant_id = _DummyColumn()
    ts = _DummyColumn()
    event_name = _DummyColumn()
    channel = _DummyColumn()
    metadata_payload = _DummyColumn()
    session_id = _DummyColumn()


class AdminWhatsappFunnelContractTestCase(unittest.TestCase):
    def test_contract_version_constant(self):
        self.assertEqual(WHATSAPP_FUNNEL_CONTRACT_VERSION, "admin.analytics.whatsapp_funnel.v1")

    def test_payload_includes_contract_version(self):
        filters = SimpleNamespace(
            tenant_id="12",
            scope="tenant",
            date_from=None,
            date_to=None,
            canales=[],
            categorias=[],
            estados=[],
            bbox=None,
            resolution="",
        )

        with patch("routes.admin_analytics._analytics_event_tenant_id", return_value=12), patch(
            "routes.admin_analytics.AnalyticsEventV2", _DummyAnalyticsEventV2
        ):
            payload = _build_whatsapp_funnel_payload(filters)

        self.assertEqual(payload["contract_version"], WHATSAPP_FUNNEL_CONTRACT_VERSION)
        self.assertIn("stages", payload)
        stage_names = {stage["event_name"] for stage in payload["stages"]}
        self.assertIn("whatsapp_catalog_viewed", stage_names)
        self.assertIn("whatsapp_checkout_session_created", stage_names)
        self.assertIn("whatsapp_payment_webhook_confirmed", stage_names)


if __name__ == "__main__":
    unittest.main()
