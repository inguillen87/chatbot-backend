"""Full SPA/Flask comparison acceptance on disposable data and identities."""
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
    from models import EncPregunta, EncOpcion, EncRespuesta, EncRespuestaDetalle
    runtime = SurveyAcceptanceServer()
    populated = runtime.create_survey('publicada', responses=8, title='Comparación QA desechable')
    empty = runtime.create_survey('publicada', title='Comparación QA sin respuestas')
    with runtime.app.app_context():
        question = EncPregunta(encuesta_id=populated['id'], orden=2,
            tipo='single_choice', texto='¿Elegís esta propuesta?', obligatoria=False)
        db.session.add(question)
        db.session.flush()
        yes = EncOpcion(pregunta_id=question.id, orden=1, texto='Sí')
        no = EncOpcion(pregunta_id=question.id, orden=2, texto='No')
        db.session.add_all([yes, no])
        db.session.flush()
        records = EncRespuesta.query.filter_by(encuesta_id=populated['id']).order_by(EncRespuesta.id).all()
        for index, record in enumerate(records):
            record.canal = 'web' if index < 4 else 'whatsapp'
            record.barrio = 'Centro QA' if index in (0, 3, 4, 5) else 'Norte QA'
            record.genero = 'no_informado'
            option = yes if index in (0, 1, 2, 4) else no
            db.session.add(EncRespuestaDetalle(respuesta_id=record.id, pregunta_id=question.id, opcion_id=option.id))
        # Historic repeated rows must not turn one respondent into two selections.
        db.session.add(EncRespuestaDetalle(respuesta_id=records[0].id, pregunta_id=question.id, opcion_id=yes.id))
        populated.update(question_id=question.id, yes_id=yes.id, no_id=no.id)
        db.session.commit()

    def snapshot():
        with runtime.app.app_context():
            return {
                'responses': [tuple(row) for row in db.session.query(EncRespuesta.id,
                    EncRespuesta.encuesta_id, EncRespuesta.canal, EncRespuesta.barrio).order_by(EncRespuesta.id).all()],
                'details': [tuple(row) for row in db.session.query(EncRespuestaDetalle.id,
                    EncRespuestaDetalle.respuesta_id, EncRespuestaDetalle.pregunta_id,
                    EncRespuestaDetalle.opcion_id).order_by(EncRespuestaDetalle.id).all()],
                'surveys': {item['id']: runtime.read_storage(item['id']) for item in (populated, empty)},
            }

    runner = frontend / '.vercel/segment-compare-full.browser.mjs'
    runner.parent.mkdir(parents=True, exist_ok=True)
    if runner.exists():
        runtime.close()
        raise RuntimeError('Refusing to replace an existing browser runner')
    shutil.copyfile(Path(__file__).with_name('segment-compare-full.browser.mjs'), runner)
    before = snapshot()
    env = {**os.environ, 'SEGMENT_API': runtime.origin,
           'SEGMENT_ACCOUNT': runtime.accounts['acceptance-a']['email'],
           'SEGMENT_PASSWORD': runtime.password,
           'SEGMENT_CASES': json.dumps({'populated': populated, 'empty': empty})}
    try:
        subprocess.run(['node', str(runner)], cwd=frontend, env=env, check=True, timeout=360)
        if before != snapshot() or runtime.mutations:
            raise AssertionError('Read-only comparison changed survey data')
        print(json.dumps({'read_only_storage_unchanged': True, 'surveys': 2}))
    finally:
        runner.unlink(missing_ok=True)
        runtime.close()


if __name__ == '__main__':
    main()
