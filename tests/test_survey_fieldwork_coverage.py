from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from services.survey_fieldwork_coverage import DIMENSIONS, build_fieldwork_coverage
from tests.test_survey_analytics_evidence import summary_fixture

SURVEY = SimpleNamespace(id=301, tenant_id=7)


def counts(total=200, recorded=1, mode="real"):
    return {"survey_id": 301, "tenant_id": 7, "mode": mode,
            "selected_records": total, "recorded": {key: recorded for key, *_ in DIMENSIONS}}


class FieldworkCoverageTests(unittest.TestCase):
    def test_exact_denominator_and_low_percent(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(), counts())
        self.assertEqual(result["basis"], {"selected_records": 200, "exact": True})
        for item in result["dimensions"]:
            self.assertEqual((item["recorded_count"], item["missing_count"], item["coverage_percent"]), (1, 199, 0.5))
        self.assertFalse(result["inference_authorized"])
        self.assertIsNone(result["response_rate"])

    def test_missing_fields_are_zero_with_nonempty_base(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(), counts(recorded=0))
        self.assertTrue(all(row["coverage_percent"] == 0 for row in result["dimensions"]))

    def test_empty_base_has_no_percent(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(0, complete=0), counts(0, 0))
        self.assertTrue(all(row["coverage_percent"] is None for row in result["dimensions"]))

    def test_sample_limit_is_irrelevant(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(1000, sample=0), counts(1000, 250))
        self.assertTrue(all(row["coverage_percent"] == 25 for row in result["dimensions"]))
        self.assertTrue(result["basis"]["exact"])

    def test_synthetic_has_explicit_limit(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(mode="synthetic"), counts(mode="synthetic"))
        self.assertEqual(result["scope"]["mode"], "synthetic")
        self.assertEqual(result["limitations"][0]["id"], "synthetic")
        self.assertTrue(result["ui"]["description"].startswith("Datos sintéticos de demostración."))

    def test_filter_values_and_raw_aggregate_properties_never_escape(self):
        aggregate = counts(); aggregate["phone"] = "private-phone-value"
        result = build_fieldwork_coverage(SURVEY, summary_fixture(), aggregate,
            {"genero": "private-filter-value", "unknown": "private-token"})
        self.assertTrue(result["scope"]["filtered"])
        rendered = json.dumps(result)
        for value in ("private-phone-value", "private-filter-value", "private-token"):
            self.assertNotIn(value, rendered)

    def test_data_mode_is_not_a_segmentation_filter(self):
        result = build_fieldwork_coverage(SURVEY, summary_fixture(), counts(), {"data_mode": "real", "desde": ""})
        self.assertFalse(result["scope"]["filtered"])

    def test_count_mismatch_is_withheld(self):
        self.assertIsNone(build_fieldwork_coverage(SURVEY, summary_fixture(), counts(201)))

    def test_cross_context_and_modes_are_withheld(self):
        for updates in ({"survey_id": 302}, {"tenant_id": 8}, {"mode": "synthetic"}, {"survey_id": True}):
            with self.subTest(updates=updates):
                aggregate = counts(); aggregate.update(updates)
                self.assertIsNone(build_fieldwork_coverage(SURVEY, summary_fixture(), aggregate))

    def test_invalid_counts_are_not_clamped(self):
        for value in (-1, 201, True, None, "1", 0.5, float("nan"), float("inf"), 9007199254740992):
            with self.subTest(value=value):
                aggregate = counts(); aggregate["recorded"]["gender"] = value
                self.assertIsNone(build_fieldwork_coverage(SURVEY, summary_fixture(), aggregate))

    def test_missing_or_extra_dimensions_are_rejected(self):
        for change in ("missing", "extra"):
            aggregate = counts()
            if change == "missing": del aggregate["recorded"]["country"]
            else: aggregate["recorded"]["email"] = 1
            self.assertIsNone(build_fieldwork_coverage(SURVEY, summary_fixture(), aggregate))

    def test_untrusted_summary_is_withheld(self):
        for updates in ({"mode": "synthetic"}, {"server_trusted_classification": False},
                {"exact_aggregates": False}, {"population_size": 201},
                {"unverified_responses_included": 1}, {"real_responses_included": True},
                {"contains_synthetic": True}):
            with self.subTest(updates=updates):
                summary = summary_fixture(); summary["data_provenance"].update(updates)
                self.assertIsNone(build_fieldwork_coverage(SURVEY, summary, counts()))

    def test_inputs_not_mutated(self):
        summary, aggregate, filters = summary_fixture(), counts(), {"canal": "web"}
        before = deepcopy((summary, aggregate, filters))
        build_fieldwork_coverage(SURVEY, summary, aggregate, filters)
        self.assertEqual((summary, aggregate, filters), before)

    def test_bad_context_or_payload_is_withheld(self):
        for survey, summary, aggregate in ((SimpleNamespace(id=0, tenant_id=7), summary_fixture(), counts()),
                (SURVEY, None, counts()), (SURVEY, summary_fixture(), None),
                (SURVEY, {**summary_fixture(), "encuesta_id": 999}, counts())):
            self.assertIsNone(build_fieldwork_coverage(survey, summary, aggregate))


if __name__ == "__main__": unittest.main(verbosity=2)
