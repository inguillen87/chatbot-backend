import unittest

from routes.analytics import _compute_identity_coverage, _coverage_slo_status


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

    def test_handles_empty_event_list(self):
        result = _compute_identity_coverage([])
        self.assertEqual(result["total_events"], 0)
        self.assertEqual(result["events_with_identity"], 0)
        self.assertEqual(result["coverage_pct"], 0.0)
        self.assertEqual(result["channels"], {})


if __name__ == "__main__":
    unittest.main()
