"""Actual survey SPA + full disposable backend. No customer endpoints or keys."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

FRONTEND_REVISION = 'ec63a2e1e1d5d1c2b2856b54d48dfe4e5804e196'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', required=True)
    args = parser.parse_args()
    frontend = Path(args.frontend).resolve()
    revision = subprocess.check_output(['git', '-C', str(frontend), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != FRONTEND_REVISION:
        raise RuntimeError('Use the reviewed survey workspace revision')
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer
    runtime = SurveyAcceptanceServer()
    runner = frontend / '.vercel' / 'survey-workspace.browser.mjs'
    runner.parent.mkdir(parents=True, exist_ok=True)
    if runner.exists():
        runtime.close()
        raise RuntimeError('Do not overwrite an existing acceptance runner')
    shutil.copyfile(Path(__file__).with_name('survey-workspace.browser.mjs'), runner)
    environment = {**os.environ,
        'SURVEY_TEST_ORIGIN': runtime.origin,
        'SURVEY_TEST_ACCOUNTS': json.dumps(runtime.accounts),
        'SURVEY_TEST_PASSWORD': runtime.password,
        'SURVEY_TEST_CONTROL': runtime.token,
        'SURVEY_TEST_CASES': json.dumps(runtime.cases),
    }
    try:
        result = subprocess.run(['node', str(runner)], cwd=frontend, env=environment, timeout=360)
        if result.returncode:
            raise RuntimeError('Survey workspace browser acceptance failed')
        persistence = []
        for scenario, item in runtime.cases.items():
            actual = runtime.read_storage(item['id'])
            expected = {'exists': True, 'state': 'cerrada', 'responses': item['responses']} if scenario.startswith('close') else {
                'exists': False, 'state': None, 'responses': 0}
            if actual != expected:
                raise AssertionError('Actual persistent survey outcome differs from browser confirmation')
            persistence.append({'scenario': scenario, **actual})
        evidence = {'frontend_revision': revision, 'original_models_and_auth': True,
                    'synthetic_accounts_and_sqlite': True, 'persistent_outcomes': persistence}
        destination = frontend / 'test-evidence' / 'survey-workspace' / 'persistence.json'
        destination.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence))
    finally:
        runner.unlink(missing_ok=True)
        runtime.close()


if __name__ == '__main__':
    main()
