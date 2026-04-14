import unittest

from routes.analytics import _build_identity_alert_event_payloads, _build_identity_alerts, _compute_identity_coverage, _coverage_slo_status, _parse_channel_targets


class AnalyticsIdentityCoverageTestCase(unittest.TestCase):
    def test_computes_global_and_channel_coverage(self):
        events = [
            {
                "channel": "whatsapp",
                "metadata": {"contact_key": "ck-1"},
                "session_id": None,
                "anon_id": None,
            },
            {
                "channel": "whatsapp",
                "metadata": {},
                "session_id": None,
                "anon_id": "anon-1",
            },
            {
                "channel": "web",
                "metadata": {},
                "session_id": None,
                "anon_id": None,
            },
        ]

        result = _compute_identity_coverage(events)

        self.assertEqual(result["total_events"], 3)
        self.assertEqual(result["events_with_identity"], 2)
        self.assertEqual(result["coverage_pct"], 66.67)
        self.assertEqual(result["channels"]["whatsapp"]["coverage_pct"], 100.0)
        self.assertEqual(result["channels"]["web"]["coverage_pct"], 0.0)

    def test_coverage_slo_status(self):
        self.assertEqual(_coverage_slo_status(92.0, 90), "ok")
        self.assertEqual(_coverage_slo_status(80.0, 90), "below_target")

    def test_parse_channel_targets(self):
        parsed = _parse_channel_targets("whatsapp:95,web:85", 90.0)
        self.assertEqual(parsed["whatsapp"], 95.0)
        self.assertEqual(parsed["web"], 85.0)

    def test_build_identity_alerts(self):
        channels = {
            "whatsapp": {"coverage_pct": 82.5},
            "web": {"coverage_pct": 95.0},
        }
        alerts = _build_identity_alerts(
            channels,
            target_pct=90.0,
            channel_targets={"web": 98.0},
        )
        self.assertEqual(len(alerts), 2)
        self.assertEqual(alerts[0]["type"], "identity_coverage_below_target")
        self.assertEqual(alerts[0]["recommended_action"], "increase_contact_key_propagation")

    def test_build_identity_alert_event_payloads(self):
        alerts = [{"type": "identity_coverage_below_target", "channel": "whatsapp", "coverage_pct": 80.0, "target_pct": 95.0, "gap_pct": 15.0, "recommended_action": "increase_contact_key_propagation"}]
        payloads = _build_identity_alert_event_payloads(
            tenant_id=12,
            alerts=alerts,
            target_pct=90.0,
            overall_coverage_pct=82.5,
        )
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["event_name"], "identity_coverage_alert")
        self.assertEqual(payloads[0]["tenant_id"], 12)
        self.assertEqual(payloads[0]["payload"]["overall_coverage_pct"], 82.5)

    def test_handles_empty_event_list(self):
        result = _compute_identity_coverage([])
        self.assertEqual(result["total_events"], 0)
        self.assertEqual(result["events_with_identity"], 0)
        self.assertEqual(result["coverage_pct"], 0.0)
        self.assertEqual(result["channels"], {})


if __name__ == "__main__":
    unittest.main()
