"""Run the pinned real SPA against a full disposable Flask application."""
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
    expected = 'b0c3ba0382b26d8a45014c84e2c25c90ce57a2a3'
    revision = subprocess.check_output(['git', '-C', str(frontend), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != expected:
        raise RuntimeError('Acceptance requires the reviewed frontend revision')
    from tests.profile_acceptance_runtime import prepare_process, create_disposable_app
    prepare_process()
    from database import db
    from models import AuditEvent, MessageTemplateRegistry
    from werkzeug.serving import make_server
    with tempfile.TemporaryDirectory(prefix='chatboc-whatsapp-spa-') as directory:
        app, accounts, password = create_disposable_app(directory)
        app.config['BACKEND_VERSION'] = 'a' * 40
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        destination = frontend / '.vercel' / 'whatsapp-draft-http.browser.mjs'
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            if destination.exists():
                raise RuntimeError('Do not overwrite a previous acceptance runner')
            shutil.copyfile(Path(__file__).with_name('whatsapp-draft-http.browser.mjs'), destination)
            created = True
            env = {**os.environ, 'PROFILE_BACKEND_ORIGIN': f'http://127.0.0.1:{server.server_port}',
                'PROFILE_TEST_ACCOUNTS': json.dumps(accounts), 'PROFILE_TEST_PASSWORD': password,
                'VITE_EXPECTED_BACKEND_REVISION': 'a' * 40}
            result = subprocess.run(['node', str(destination)], cwd=frontend, env=env, timeout=240)
            if result.returncode:
                raise RuntimeError('Actual WhatsApp panel acceptance failed')
            with app.app_context():
                drafts = MessageTemplateRegistry.query.filter_by(tenant_id=accounts['acceptance-a']['tenant_id'],
                    provider='chatboc', channel='whatsapp').all()
                events = AuditEvent.query.filter_by(tenant_id=accounts['acceptance-a']['tenant_id'],
                    event_type='whatsapp_template_pack.local_drafts_materialized').all()
                assert drafts and len(events) == 1
                assert all(row.status == 'local_draft' and not row.content_sid and not row.external_template_id for row in drafts)
            print(json.dumps({'frontend_revision': revision, 'actual_spa': True, 'full_flask_app': True,
                'local_drafts': len(drafts), 'creation_audit_events': len(events),
                'synthetic_accounts_and_database': True, 'provider_requests_performed': False}))
        finally:
            if created:
                destination.unlink(missing_ok=True)
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    main()
