"""Full application HTTP evidence checks on disposable identities and SQLite."""
from tests.profile_acceptance_runtime import prepare_process

if __name__ == '__main__':
    prepare_process()

import os
import unittest
from unittest.mock import patch
from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer, Browser


class AnalyticsEvidenceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from database import db
        from models import EncPregunta, EncRespuesta, EncRespuestaDetalle
        cls.runtime = SurveyAcceptanceServer()
        cls.record = cls.runtime.create_survey('publicada', responses=200, title='QA evidencia descriptiva')
        with cls.runtime.app.app_context():
            question = EncPregunta.query.filter_by(encuesta_id=cls.record['id']).one()
            question.obligatoria = True
            response = EncRespuesta.query.filter_by(encuesta_id=cls.record['id']).order_by(EncRespuesta.id.desc()).first()
            db.session.add(EncRespuestaDetalle(respuesta_id=response.id, pregunta_id=question.id, texto_libre='Respuesta de prueba'))
            db.session.commit()
        cls.empty = cls.runtime.create_survey('publicada', responses=0, title='QA sin respuestas')

    @classmethod
    def tearDownClass(cls):
        cls.runtime.close()

    def login(self, account='acceptance-a'):
        browser=Browser(self.runtime.origin)
        status,body=browser.request('POST','/auth/login',{
            'email':self.runtime.accounts[account]['email'],'password':self.runtime.password})
        self.assertEqual(status,200)
        self.assertTrue(body.get('token'))
        return browser

    def endpoint(self, sid=None, suffix='resumen', prefix='/api/admin/encuestas', extra=''):
        return f"{prefix}/{sid or self.record['id']}/analytics/{suffix}?tenant_slug=acceptance-a{extra}"

    def test_real_summary_low_completion_has_correct_denominator(self):
        status,body=self.login().request('GET',self.endpoint())
        self.assertEqual(status,200)
        self.assertEqual(body['total_respuestas'],200)
        self.assertEqual(body['respuestas_completas'],1)
        self.assertEqual(body['tasa_completitud'],0.5)
        evidence=body['analytics_evidence']
        self.assertEqual(evidence['cards'][2]['value'],0.5)
        self.assertEqual(evidence['cards'][2]['basis'],'observed')
        self.assertEqual(evidence['scope']['tenant_id'],self.record['tenant_id'])
        self.assertFalse(evidence['inference_authorized'])

    def test_all_authorized_summary_aliases_publish_the_same_contract(self):
        browser=self.login()
        digests=set()
        for prefix in ['/api/encuestas','/admin/encuestas','/api/admin/encuestas','/api/municipal/encuestas']:
            for suffix in ['summary','resumen']:
                with self.subTest(prefix=prefix,suffix=suffix):
                    status,body=browser.request('GET',self.endpoint(prefix=prefix,suffix=suffix))
                    self.assertEqual(status,200)
                    digests.add(body['analytics_evidence']['evidence_revision'])
        self.assertEqual(len(digests),1)

    def test_latest_subset_is_not_marked_observed_completion(self):
        with patch.dict(os.environ,{'SURVEY_ANALYTICS_SAMPLE_LIMIT':'1'}):
            status,body=self.login().request('GET',self.endpoint())
        self.assertEqual(status,200)
        evidence=body['analytics_evidence']
        self.assertEqual(evidence['basis']['detail_records'],1)
        self.assertEqual(evidence['basis']['selected_records'],200)
        self.assertEqual(evidence['cards'][2]['basis'],'estimated')
        self.assertEqual(evidence['basis']['detail_coverage_percent'],0.5)
        self.assertIsNone(evidence['margin_of_error'])

    def test_empty_summary_does_not_fabricate_a_zero_percent_rate(self):
        status,body=self.login().request('GET',self.endpoint(self.empty['id']))
        self.assertEqual(status,200)
        self.assertIsNone(body['analytics_evidence']['cards'][2]['value'])
        self.assertIsNone(body['analytics_evidence']['basis']['detail_coverage_percent'])

    def test_active_filter_is_named_but_raw_value_is_not_copied(self):
        status,body=self.login().request('GET',self.endpoint(extra='&canal=private-filter-value'))
        self.assertEqual(status,200)
        evidence=body['analytics_evidence']
        self.assertTrue(evidence['scope']['filtered'])
        import json
        self.assertNotIn('private-filter-value',json.dumps(evidence))
        self.assertEqual(evidence['basis']['selected_records'],0)

    def test_anonymous_access_stays_unauthorized(self):
        status,body=Browser(self.runtime.origin).request('GET',self.endpoint())
        self.assertEqual(status,401)
        self.assertNotIn('analytics_evidence',body)

    def test_foreign_tenant_cannot_obtain_evidence(self):
        status,body=self.login('acceptance-b').request('GET',self.endpoint().replace('acceptance-a','acceptance-b'))
        self.assertEqual(status,403)
        self.assertNotIn('analytics_evidence',body)

    def test_employee_without_sensitive_data_capability_cannot_obtain_evidence(self):
        status,body=self.login('viewer').request('GET',self.endpoint())
        self.assertEqual(status,403)
        self.assertNotIn('analytics_evidence',body)

    def test_missing_survey_is_not_a_zero_response_report(self):
        status,body=self.login().request('GET',self.endpoint(2147483000))
        self.assertEqual(status,404)
        self.assertNotIn('analytics_evidence',body)

    def test_read_keeps_the_original_authenticated_session(self):
        browser=self.login()
        self.assertEqual(browser.request('GET',self.endpoint())[0],200)
        self.assertEqual(browser.request('GET','/api/me?tenant_slug=acceptance-a')[0],200)

    def test_dashboard_attaches_evidence_without_changing_summary_counts(self):
        status,body=self.login().request('GET',self.endpoint(suffix='dashboard',extra='&fast=1'))
        self.assertEqual(status,200)
        summary=body['modules']['summary']
        self.assertEqual(summary['analytics_evidence']['cards'][2]['value'],0.5)
        self.assertEqual(summary['total_respuestas'],200)

    def test_public_summary_service_does_not_get_private_disclosure(self):
        from services.encuestas_analytics_service import get_summary
        with self.runtime.app.test_request_context('/'):
            body=get_summary(self.record['id'])
        self.assertNotIn('analytics_evidence',body)


if __name__=='__main__':unittest.main(verbosity=2)
