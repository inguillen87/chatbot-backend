"""Full SPA methodology workflow against disposable accounts and database only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--frontend',required=True)
    parser.add_argument('--frontend-revision')
    args=parser.parse_args()
    frontend=Path(args.frontend).resolve()
    sha=subprocess.check_output(['git','rev-parse','HEAD'],cwd=frontend,text=True).strip()
    if args.frontend_revision and sha!=args.frontend_revision:
        raise RuntimeError('Unexpected frontend revision')
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer
    runtime=SurveyAcceptanceServer()
    runtime.app.config['SURVEY_METHODOLOGY_ENABLED']=True
    cases=[runtime.create_survey(title='Methodology browser QA '+str(width)) for width in (1440,820,390,320)]
    runner=frontend/'.vercel/methodology-full.browser.mjs'
    runner.parent.mkdir(parents=True,exist_ok=True)
    if runner.exists():
        runtime.close();raise RuntimeError('A browser runner already exists')
    shutil.copyfile(Path(__file__).with_name('methodology-full.browser.mjs'),runner)
    env={**os.environ,'METHOD_API':runtime.origin,'METHOD_ACCOUNT':runtime.accounts['acceptance-a']['email'],
        'METHOD_PASSWORD':runtime.password,'METHOD_CASES':json.dumps(cases),'PYTHONIOENCODING':'utf-8'}
    try:
        result=subprocess.run(['node',str(runner)],cwd=frontend,env=env,timeout=300)
        if result.returncode:
            raise RuntimeError('Full methodology browser run failed: '+str(result.returncode))
        from models import SurveyMethodologyRevision,AuditEvent
        checks=[]
        with runtime.app.app_context():
            for case in cases:
                count=SurveyMethodologyRevision.query.filter_by(survey_id=case['id'],tenant_id=case['tenant_id']).count()
                audits=AuditEvent.query.filter_by(resource_id=str(case['id']),event_type='survey.methodology.revision_saved').count()
                if count!=2 or audits!=2:raise AssertionError('Expected exactly two versions and two audit entries')
                checks.append({'survey_id':case['id'],'versions':count,'audit_events':audits})
        for case in cases:
            if runtime.read_storage(case['id'])!={'exists':True,'state':'borrador','responses':0}:
                raise AssertionError('Study metadata workflow changed a questionnaire or its responses')
        report={'frontend_revision':sha,'synthetic_accounts_and_sqlite':True,'original_auth_and_models':True,
                'questionnaires_and_responses_unchanged':True,'checks':checks}
        output=frontend/'test-evidence/methodology-full/persistence.json'
        output.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
    finally:
        runner.unlink(missing_ok=True)
        runtime.close()


if __name__=='__main__':main()
