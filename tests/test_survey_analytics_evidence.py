from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from services.survey_analytics_evidence import build_analytics_evidence

SURVEY = SimpleNamespace(id=301, tenant_id=7)


def summary_fixture(total=200, sample=None, complete=1, mode='real'):
    sample = min(total, 500) if sample is None else sample
    partial = sample < total
    return {'encuesta_id':301, 'total_respuestas':total, 'participantes_unicos':total,
        'respuestas_completas':complete, 'tasa_completitud':round(complete/total*100,2) if total else 0,
        'data_provenance': {'contract_version':'surveys.response_provenance.v1',
            'server_trusted_classification':True, 'exact_aggregates':True,
            'mode':mode, 'contains_synthetic':mode=='synthetic' and total>0,
            'population_size':total, 'sample_size':sample, 'sample_limit':500,
            'sample_order':'latest', 'partial':partial, 'sampled':partial,
            'completion_estimated_from_sample':partial, 'eligibility_estimated_from_sample':partial,
            'real_responses_included':total if mode=='real' else 0,
            'synthetic_responses_included':total if mode=='synthetic' else 0,
            'synthetic_responses_excluded':5 if mode=='real' else 0,
            'unverified_responses_included':0, 'unverified_responses_excluded':2}}


class AnalyticsEvidenceTests(unittest.TestCase):
    def test_low_percentage_is_not_a_fraction(self):
        report=build_analytics_evidence(SURVEY,summary_fixture())
        self.assertEqual(report['cards'][2]['value'],0.5)
        self.assertEqual(report['cards'][2]['unit'],'percent')
        self.assertEqual(report['cards'][2]['basis'],'observed')

    def test_partial_analysis_is_explicitly_estimated(self):
        report=build_analytics_evidence(SURVEY,summary_fixture(1200,500,960))
        self.assertEqual(report['cards'][0]['basis'],'observed')
        self.assertEqual(report['cards'][2]['basis'],'estimated')
        self.assertEqual(report['basis']['detail_coverage_percent'],41.67)
        self.assertIn('recent_subset',[n['id'] for n in report['limitations']])

    def test_empty_base_is_unavailable_not_zero_percent(self):
        report=build_analytics_evidence(SURVEY,summary_fixture(0,0,0))
        self.assertIsNone(report['cards'][2]['value'])
        self.assertEqual(report['cards'][2]['basis'],'unavailable')
        self.assertIsNone(report['basis']['detail_coverage_percent'])

    def test_disabled_detail_scan_has_no_completion_estimate(self):
        report=build_analytics_evidence(SURVEY,summary_fixture(200,0,0))
        self.assertIsNone(report['cards'][2]['value'])

    def test_synthetic_mode_is_never_real_participation(self):
        report=build_analytics_evidence(SURVEY,summary_fixture(mode='synthetic'))
        self.assertEqual(report['scope']['mode'],'synthetic')
        self.assertEqual(report['limitations'][0]['id'],'synthetic')
        self.assertFalse(report['inference_authorized'])

    def test_no_statistical_inference_or_precision_is_fabricated(self):
        report=build_analytics_evidence(SURVEY,summary_fixture(1000000,500,800000))
        self.assertFalse(report['inference_authorized'])
        self.assertIsNone(report['margin_of_error'])
        self.assertIn('precision',[n['id'] for n in report['limitations']])

    def test_filter_values_and_extra_data_are_never_copied(self):
        source=summary_fixture();source['respondent_email']='private@example.test'
        report=build_analytics_evidence(SURVEY,source,{'genero':'private-sensitive-value','unrecognized':'private-token'})
        encoded=json.dumps(report)
        self.assertNotIn('private',encoded)
        self.assertTrue(report['scope']['filtered'])

    def test_same_counts_different_active_filter_kind_produces_different_disclosure(self):
        one=build_analytics_evidence(SURVEY,summary_fixture())
        two=build_analytics_evidence(SURVEY,summary_fixture(),{'canal':'web'})
        self.assertNotEqual(one['evidence_revision'],two['evidence_revision'])
        self.assertFalse(one['scope']['filtered'])

    def test_does_not_mutate_the_summary_or_filters(self):
        source=summary_fixture();filters={'canal':'web'};before=deepcopy((source,filters))
        build_analytics_evidence(SURVEY,source,filters)
        self.assertEqual((source,filters),before)

    def test_revision_is_deterministic(self):
        self.assertEqual(build_analytics_evidence(SURVEY,summary_fixture()),build_analytics_evidence(SURVEY,summary_fixture()))

    def test_unknown_or_missing_provenance_is_not_verified(self):
        for p in [None,{},[],{'contract_version':'unknown'}]:
            source=summary_fixture();source['data_provenance']=p
            self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_suppressed_counts_stay_suppressed(self):
        source=summary_fixture();source['data_provenance']['population_size']=None
        self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_conflicting_id_or_missing_tenant_is_rejected(self):
        for survey in [SimpleNamespace(id=302,tenant_id=7),SimpleNamespace(id=301,tenant_id=None),SimpleNamespace(id=True,tenant_id=7)]:
            self.assertIsNone(build_analytics_evidence(survey,summary_fixture()))

    def test_impossible_sample_metadata_is_rejected(self):
        for updates in [{'sample_size':201},{'sample_limit':2},{'partial':True},{'sampled':1},
                        {'sample_order':'random'},{'completion_estimated_from_sample':True}]:
            source=summary_fixture();source['data_provenance'].update(updates)
            with self.subTest(updates=updates):self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_nonfinite_negative_boolean_and_string_counts_are_rejected(self):
        for value in [-1,True,0.5,'200',float('nan'),float('inf'),2**53]:
            source=summary_fixture();source['data_provenance']['population_size']=value
            with self.subTest(value=value):self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_mismatched_origins_never_become_a_valid_mixture(self):
        for updates in [{'unverified_responses_included':1},{'synthetic_responses_included':1},
                        {'real_responses_included':199},{'contains_synthetic':True},{'server_trusted_classification':False}]:
            source=summary_fixture();source['data_provenance'].update(updates)
            with self.subTest(updates=updates):self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_counts_cannot_exceed_denominator(self):
        for key in ['participantes_unicos','respuestas_completas']:
            source=summary_fixture();source[key]=201
            self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_percentage_needs_matching_numerator_and_denominator(self):
        for rate in [50,-1,101,float('nan'),float('inf'),10**500,True,'0.5']:
            source=summary_fixture();source['tasa_completitud']=rate
            with self.subTest(rate=rate):self.assertIsNone(build_analytics_evidence(SURVEY,source))

    def test_public_service_is_not_modified_to_emit_private_evidence(self):
        root=Path(__file__).resolve().parents[1]
        self.assertNotIn('build_analytics_evidence',(root/'services/encuestas_analytics_service.py').read_text(encoding='utf-8'))
        self.assertNotIn('analytics_evidence',(root/'routes/encuestas_public.py').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main(verbosity=2)
