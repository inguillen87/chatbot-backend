"""Run the actual template library against full Flask with disposable identities."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', required=True)
    args = parser.parse_args()
    frontend = Path(args.frontend).resolve()
    revision = subprocess.check_output(['git', '-C', str(frontend), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != '3379f2dd58628d1becb16ec2b22f823354215a90':
        raise RuntimeError('This acceptance requires its reviewed frontend revision')
    from tests.profile_acceptance_runtime import prepare_process, create_disposable_app
    prepare_process()
    directory = tempfile.TemporaryDirectory(prefix='chatboc-template-library-')
    app, accounts, password = create_disposable_app(directory.name)
    from werkzeug.serving import make_server
    from models import MessageTemplateRegistry
    from database import db
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    destination = frontend / '.vercel' / 'template-library-http.browser.mjs'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError('Refusing to overwrite another acceptance runner')
    shutil.copyfile(Path(__file__).with_name('template-library-http.browser.mjs'), destination)
    env = {**os.environ, 'VITE_PROXY_TARGET': origin, 'VITE_BACKEND_URL': '/api',
        'VITE_BACKEND_BOOTSTRAP_GATE_ENABLED': 'false',
        'CHATBOC_ACCEPTANCE_EMAIL': accounts['acceptance-a']['email'],
        'CHATBOC_ACCEPTANCE_SECOND_EMAIL': accounts['second']['email'],
        'CHATBOC_ACCEPTANCE_PASSWORD': password}
    try:
        result = subprocess.run(['node', str(destination)], cwd=frontend, env=env, timeout=180)
        if result.returncode:
            raise RuntimeError('Template library paired browser acceptance failed')
        with app.app_context():
            first = MessageTemplateRegistry.query.filter_by(tenant_id=accounts['acceptance-a']['tenant_id'], provider='chatboc').count()
            other = MessageTemplateRegistry.query.filter_by(tenant_id=accounts['acceptance-b']['tenant_id']).count()
            assert first == 5, f'Expected one complete five-template draft pack, received {first}'
            assert other == 0, 'Another tenant must remain untouched'
        print(json.dumps({'frontend_revision': revision, 'full_flask_app': True,
            'persisted_local_drafts': first, 'foreign_tenant_drafts': other, 'external_providers_used': False}))
    finally:
        destination.unlink(missing_ok=True)
        server.shutdown(); thread.join(timeout=5); server.server_close()
        with app.app_context():
            db.session.remove(); db.engine.dispose()
        directory.cleanup()


if __name__ == '__main__':
    main()
