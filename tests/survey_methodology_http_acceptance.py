"""Real login, scope, version writes and audit in an isolated SQLite application."""
from tests.profile_acceptance_runtime import prepare_process
if __name__=='__main__':prepare_process()

import json
import unittest
from unittest.mock import patch
from sqlalchemy.exc import OperationalError
from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer,Browser
from tests.test_survey_methodology_contract import request as body_for
from services.survey_methodology_contract import blank_fields


class MethodologyHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime=SurveyAcceptanceServer()
        cls.runtime.app.config['SURVEY_METHODOLOGY_ENABLED']=True
    @classmethod
    def tearDownClass(cls):cls.runtime.close()
    def setUp(self):
        self.runtime.app.config.update(SURVEY_METHODOLOGY_ENABLED=True,CUTOVER_WRITER_FENCE_ENABLED=False)
        self.item=self.runtime.create_survey(title='Methodology QA instrument')
    def tearDown(self):
        self.runtime.app.config.update(SURVEY_METHODOLOGY_ENABLED=True,CUTOVER_WRITER_FENCE_ENABLED=False)
    def login(self,account='acceptance-a'):
        browser=Browser(self.runtime.origin)
        status,data=browser.request('POST','/auth/login',{'email':self.runtime.accounts[account]['email'],'password':self.runtime.password})
        self.assertEqual(status,200);return browser
    def url(self,extra='',prefix='/api/admin/encuestas'):
        return f"{prefix}/{self.item['id']}/methodology?tenant_slug=acceptance-a{extra}"
    def body(self,revision=0,instrument=1,purpose='Conocer necesidades de servicios'):
        fields=blank_fields();fields['purpose']=purpose
        return body_for(fields,revision,instrument)
    def counts(self):
        from database import db
        from models import SurveyMethodologyRevision,AuditEvent
        with self.runtime.app.app_context():
            return (SurveyMethodologyRevision.query.filter_by(survey_id=self.item['id']).count(),
                AuditEvent.query.filter_by(resource_id=str(self.item['id']),event_type='survey.methodology.revision_saved').count())

    def test_get_never_creates_a_declaration_or_guesses_fields(self):
        status,data=self.login().request('GET',self.url())
        self.assertEqual(status,200);self.assertTrue(data['available'])
        self.assertEqual(data['profile']['revision'],0);self.assertEqual(data['profile']['fields'],blank_fields())
        self.assertEqual(data['coverage']['documented'],0);self.assertEqual(self.counts(),(0,0))

    def test_save_persists_a_version_and_audit_without_changing_survey(self):
        browser=self.login();before=self.runtime.read_storage(self.item['id'])
        status,data=browser.request('PUT',self.url(),self.body())
        self.assertEqual(status,200);self.assertEqual(data['profile']['revision'],1)
        self.assertEqual(data['profile']['instrument_revision'],1);self.assertFalse(data['inference_authorized'])
        self.assertEqual(self.counts(),(1,1));self.assertEqual(self.runtime.read_storage(self.item['id']),before)
        status,reread=browser.request('GET',self.url())
        self.assertEqual(status,200);self.assertEqual(data['profile'],reread['profile'])
        self.assertEqual(browser.request('GET','/api/me?tenant_slug=acceptance-a')[0],200)

    def test_retry_after_lost_response_returns_same_version_not_duplicate(self):
        browser=self.login();body=self.body()
        first=browser.request('PUT',self.url(),body)[1];status,second=browser.request('PUT',self.url(),body)
        self.assertEqual(status,200);self.assertTrue(second['replayed'])
        self.assertEqual(first['profile'],second['profile']);self.assertEqual(self.counts(),(1,1))

    def test_stale_editor_cannot_overwrite_another_admin(self):
        first=self.login();second=self.login('second')
        self.assertEqual(first.request('PUT',self.url(),self.body())[0],200)
        status,error=second.request('PUT',self.url(),self.body(purpose='Distinct competing declaration'))
        self.assertEqual(status,409);self.assertEqual(error['reason_code'],'methodology_revision_conflict')
        self.assertEqual(self.counts(),(1,1))

    def test_history_preserves_old_content_and_marks_read_only(self):
        browser=self.login();browser.request('PUT',self.url(),self.body())
        browser.request('PUT',self.url(),self.body(1,purpose='Revised study objective'))
        status,old=browser.request('GET',self.url('&revision=1'))
        self.assertEqual(status,200);self.assertFalse(old['capabilities']['can_edit'])
        self.assertEqual(old['profile']['fields']['purpose'],'Conocer necesidades de servicios')
        self.assertEqual(old['latest_revision'],2);self.assertEqual(self.counts(),(2,2))

    def test_noop_does_not_manufacture_another_version(self):
        browser=self.login();browser.request('PUT',self.url(),self.body())
        status,result=browser.request('PUT',self.url(),self.body(1))
        self.assertEqual(status,200);self.assertTrue(result['unchanged']);self.assertEqual(self.counts(),(1,1))

    def test_questionnaire_change_requires_fresh_expected_revision(self):
        from database import db
        from models import EncEncuesta
        browser=self.login();browser.request('PUT',self.url(),self.body())
        with self.runtime.app.app_context():
            item=db.session.get(EncEncuesta,self.item['id']);item.structure_revision=2;db.session.commit()
        data=browser.request('GET',self.url())[1];self.assertTrue(data['linked_instrument_changed'])
        self.assertEqual(browser.request('PUT',self.url(),self.body(1))[0],409)
        status,result=browser.request('PUT',self.url(),self.body(1,2))
        self.assertEqual(status,200);self.assertEqual(result['profile']['instrument_revision'],2)
        self.assertFalse(result['linked_instrument_changed'])

    def test_anonymous_employee_and_other_tenant_denied(self):
        self.assertEqual(Browser(self.runtime.origin).request('GET',self.url())[0],401)
        for account in ['viewer','acceptance-b']:
            browser=self.login(account)
            url=self.url().replace('tenant_slug=acceptance-a','tenant_slug='+('acceptance-b' if account=='acceptance-b' else 'acceptance-a'))
            for method in ['GET','PUT']:
                self.assertEqual(browser.request(method,url,self.body() if method=='PUT' else None)[0],403)
        self.assertEqual(self.counts(),(0,0))

    def test_disabled_feature_does_not_query_its_table(self):
        from services import survey_methodology as svc
        self.runtime.app.config['SURVEY_METHODOLOGY_ENABLED']=False
        with patch.object(svc,'_query',side_effect=AssertionError('Must not query disabled storage')):
            browser=self.login();status,data=browser.request('GET',self.url())
            self.assertEqual(status,200);self.assertFalse(data['available'])
            self.assertEqual(browser.request('PUT',self.url(),self.body())[0],503)

    def test_schema_failure_returns_error_without_runtime_ddl_or_success(self):
        from services import survey_methodology as svc
        browser=self.login()
        with patch.object(svc,'_query',side_effect=OperationalError('SELECT',{},Exception('sensitive db detail'))):
            status,data=browser.request('GET',self.url())
        self.assertEqual(status,503);self.assertNotIn('sensitive',json.dumps(data))
        self.assertEqual(self.counts(),(0,0))

    def test_failed_audit_rolls_back_the_version(self):
        from sqlalchemy import event
        from models import AuditEvent
        def fail(mapper,connection,target):
            if target.event_type=='survey.methodology.revision_saved':
                raise OperationalError('INSERT',{},Exception('synthetic audit failure'))
        event.listen(AuditEvent,'before_insert',fail)
        try:status,data=self.login().request('PUT',self.url(),self.body())
        finally:event.remove(AuditEvent,'before_insert',fail)
        self.assertEqual(status,503);self.assertEqual(self.counts(),(0,0))

    def test_writer_fence_remains_authoritative(self):
        browser=self.login();self.runtime.app.config['CUTOVER_WRITER_FENCE_ENABLED']=True
        status,data=browser.request('PUT',self.url(),self.body())
        self.assertEqual(status,503);self.assertEqual(data['reason_code'],'cutover_writer_fence_enabled')
        self.assertEqual(self.counts(),(0,0))

    def test_all_seven_admin_aliases_read_same_version(self):
        browser=self.login();browser.request('PUT',self.url(),self.body())
        digests=set()
        for prefix in ['/api/encuestas','/admin/encuestas','/api/admin/encuestas','/api/municipal/encuestas','/api/admin/surveys','/admin/surveys','/api/municipal/surveys']:
            status,data=browser.request('GET',self.url(prefix=prefix));self.assertEqual(status,200)
            digests.add(data['profile']['digest'])
        self.assertEqual(len(digests),1)

    def test_unrecognized_input_and_large_body_are_rejected(self):
        browser=self.login()
        for body in [{**self.body(),'certified':True},{**self.body(),'change_reason':''},[],None]:
            self.assertEqual(browser.request('PUT',self.url(),body)[0],400)
        self.assertEqual(browser.request('PUT',self.url(),{'large':'x'*40000})[0],413)
        self.assertEqual(self.counts(),(0,0))

    def test_missing_history_and_instrument_are_not_empty_success(self):
        browser=self.login()
        self.assertEqual(browser.request('GET',self.url('&revision=9'))[0],404)
        self.assertEqual(browser.request('GET',self.url('&revision=bad'))[0],400)
        self.assertEqual(browser.request('GET',self.url().replace('/'+str(self.item['id'])+'/', '/2147483000/'))[0],404)

    def test_history_is_bounded_without_losing_old_versions(self):
        browser=self.login()
        for index in range(12):
            self.assertEqual(browser.request('PUT',self.url(),self.body(index,purpose=f'Objective revision {index}'))[0],200)
        status,data=browser.request('GET',self.url())
        self.assertEqual(status,200);self.assertEqual(len(data['history']),10);self.assertTrue(data['history_has_more'])
        self.assertEqual(browser.request('GET',self.url('&revision=1'))[0],200);self.assertEqual(self.counts(),(12,12))

    def test_archived_instrument_does_not_accept_retroactive_edits(self):
        from database import db
        from models import EncEncuesta
        with self.runtime.app.app_context():
            db.session.get(EncEncuesta,self.item['id']).estado='archivada';db.session.commit()
        browser=self.login();status,data=browser.request('GET',self.url())
        self.assertEqual(status,200);self.assertFalse(data['capabilities']['can_edit'])
        self.assertEqual(browser.request('PUT',self.url(),self.body())[0],409)

    def test_response_construction_failure_also_rolls_back(self):
        from services import survey_methodology as svc
        browser=self.login()
        with patch.object(svc,'_payload',side_effect=OperationalError('SELECT',{},Exception('Synthetic response dependency failure'))):
            self.assertEqual(browser.request('PUT',self.url(),self.body())[0],503)
        self.assertEqual(self.counts(),(0,0))

    def test_metadata_and_audit_dont_rewrite_questionnaire(self):
        from database import db
        from models import EncEncuesta,AuditEvent
        browser=self.login();browser.request('PUT',self.url(),self.body())
        with self.runtime.app.app_context():
            survey=db.session.get(EncEncuesta,self.item['id'])
            self.assertEqual(survey.structure_revision,1);self.assertEqual(survey.estado,'borrador')
            audit=AuditEvent.query.filter_by(resource_id=str(survey.id),event_type='survey.methodology.revision_saved').one()
            self.assertNotIn('fields',audit.details);self.assertFalse(audit.details['raw_fields_recorded'])

    def test_no_methodology_in_public_resources_or_ordinary_survey_detail(self):
        browser=self.login();browser.request('PUT',self.url(),self.body(purpose='Restricted declaration sentinel'))
        status,detail=browser.request('GET',f"/api/admin/encuestas/{self.item['id']}?tenant_slug=acceptance-a")
        self.assertEqual(status,200);self.assertNotIn('Restricted declaration sentinel',json.dumps(detail))


    def test_identical_request_by_another_admin_is_not_a_replayed_receipt(self):
        first=self.login();second=self.login('second');body=self.body()
        self.assertEqual(first.request('PUT',self.url(),body)[0],200)
        status,data=second.request('PUT',self.url(),body)
        self.assertEqual(status,409)
        self.assertEqual(data['reason_code'],'methodology_revision_conflict')
        self.assertEqual(self.counts(),(1,1))

    def test_role_is_rechecked_after_acquiring_the_instrument_lock(self):
        from services import survey_methodology as svc
        from database import db
        from models import User
        original=svc._acquire_encuesta_write_guard
        def revoke_after_guard(survey_id):
            survey=original(survey_id)
            actor=db.session.get(User,self.runtime.accounts['acceptance-a']['id'])
            actor.rol='empleado';db.session.flush()
            return survey
        browser=self.login()
        with patch.object(svc,'_acquire_encuesta_write_guard',side_effect=revoke_after_guard):
            status,data=browser.request('PUT',self.url(),self.body())
        self.assertEqual(status,403)
        self.assertEqual(data['reason_code'],'methodology_admin_required')
        self.assertEqual(self.counts(),(0,0))

    def test_corrupted_declaration_is_neither_presented_nor_extended(self):
        from database import db
        from models import SurveyMethodologyRevision
        browser=self.login()
        self.assertEqual(browser.request('PUT',self.url(),self.body())[0],200)
        with self.runtime.app.app_context():
            row=SurveyMethodologyRevision.query.filter_by(survey_id=self.item['id']).one()
            row.fields={**row.fields,'purpose':'Injected alteration without a matching digest'}
            db.session.commit()
        for method,body in [('GET',None),('PUT',self.body(1,purpose='New declaration'))]:
            status,data=browser.request(method,self.url(),body)
            self.assertEqual(status,503)
            self.assertEqual(data['reason_code'],'methodology_history_invalid')
            self.assertNotIn('Injected alteration',json.dumps(data))
        self.assertEqual(self.counts(),(1,1))


if __name__=='__main__':unittest.main(verbosity=2)
