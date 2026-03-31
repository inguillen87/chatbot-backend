import unittest
from types import SimpleNamespace
from unittest.mock import patch

from routes import admin_analytics


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def with_entities(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _FakeEventModel:
    class _Expr:
        def __eq__(self, other):
            return ("eq", other)

        def __ge__(self, other):
            return ("ge", other)

        def __le__(self, other):
            return ("le", other)

        def in_(self, values):
            return ("in", tuple(values))

    tenant_id = _Expr()
    ts = _Expr()
    event_name = _Expr()
    channel = _Expr()
    session_id = _Expr()

    def __init__(self, rows):
        self.query = _FakeQuery(rows)


class AdminWhatsappFunnelPayloadTests(unittest.TestCase):
    def _filters(self):
        return SimpleNamespace(
            tenant_id="1",
            scope="municipio",
            date_from=None,
            date_to=None,
            canales=[],
        )

    def test_conversion_uses_unique_sessions_between_stages(self):
        rows = [
            SimpleNamespace(event_name="whatsapp_portal_menu_opened", session_id="s1"),
            SimpleNamespace(event_name="whatsapp_portal_menu_opened", session_id="s1"),
            SimpleNamespace(event_name="whatsapp_video_handoff_shared", session_id="s1"),
        ]

        with patch.object(admin_analytics, "AnalyticsEventV2", _FakeEventModel(rows)):
            payload = admin_analytics._build_whatsapp_funnel_payload(self._filters(), window_minutes=60)

        stages = {stage["event_name"]: stage for stage in payload["stages"]}
        self.assertEqual(stages["whatsapp_portal_menu_opened"]["total"], 2)
        self.assertEqual(stages["whatsapp_portal_menu_opened"]["unique_sessions"], 1)
        self.assertEqual(stages["whatsapp_video_handoff_shared"]["conversion_from_prev_pct"], 100.0)

    def test_invalid_window_minutes_falls_back_to_default(self):
        with patch.object(admin_analytics, "AnalyticsEventV2", _FakeEventModel([])):
            payload = admin_analytics._build_whatsapp_funnel_payload(self._filters(), window_minutes="invalid")

        self.assertEqual(payload["window_minutes"], 60)


if __name__ == "__main__":
    unittest.main()
