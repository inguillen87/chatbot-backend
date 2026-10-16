"""Exercise the original SPA and Flask with disposable survey data only."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', required=True)
    args = parser.parse_args()
    frontend = Path(args.frontend).resolve()
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests.survey_workspace_http_acceptance import SurveyAcceptanceServer
    from database import db
    from models import EncRespuesta
    runtime = SurveyAcceptanceServer()
    populated = runtime.create_survey('publicada', responses=8, title='Cobertura QA desechable')
    empty = runtime.create_survey('publicada', title='Cobertura QA sin respuestas')
    with runtime.app.app_context():
        records = EncRespuesta.query.filter_by(encuesta_id=populated['id']).order_by(EncRespuesta.id).all()
        for index, record in enumerate(records):
            record.canal = 'web' if index < 4 else 'whatsapp'
            record.barrio = 'Centro QA' if index < 4 else None
            record.utm_campaign = 'Campaña QA' if index < 2 else None
            # Deliberately no coordinates: absence never creates map points.
        db.session.commit()
    runner = frontend / '.vercel/fieldwork-full.browser.mjs'
    runner.parent.mkdir(parents=True, exist_ok=True)
    if runner.exists():
        runtime.close()
        raise RuntimeError('Refusing to replace an existing browser runner')
    shutil.copyfile(Path(__file__).with_name('fieldwork-full.browser.mjs'), runner)
    before = {item['id']: runtime.read_storage(item['id']) for item in (populated, empty)}
    env = {**os.environ, 'FIELDWORK_API': runtime.origin,
           'FIELDWORK_ACCOUNT': runtime.accounts['acceptance-a']['email'],
           'FIELDWORK_PASSWORD': runtime.password,
           'FIELDWORK_CASES': json.dumps({'populated': populated, 'empty': empty})}
    try:
        subprocess.run(['node', str(runner)], cwd=frontend, env=env, check=True, timeout=300)
        after = {item['id']: runtime.read_storage(item['id']) for item in (populated, empty)}
        if before != after or runtime.mutations:
            raise AssertionError('Read-only coverage changed instruments or responses')
        print(json.dumps({'read_only_storage_unchanged': True, 'surveys': len(after)}))
    finally:
        runner.unlink(missing_ok=True)
        runtime.close()


if __name__ == '__main__':
    main()
