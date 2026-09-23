"""Original SPA + Flask organization access; disposable local identities only."""
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
    parser.add_argument('--frontend', type=Path, required=True)
    frontend = parser.parse_args().frontend.resolve()
    from tests.profile_acceptance_runtime import prepare_process, create_disposable_app
    prepare_process()
    from database import db
    from models import TenantProfile, EncRespuesta, MunicipioTicket, TenantTicket, PymeTicket, User
    from werkzeug.serving import make_server
    with tempfile.TemporaryDirectory(prefix='chatboc-organization-access-') as directory:
        app, accounts, password = create_disposable_app(directory)
        with app.app_context():
            tenant = db.session.get(TenantProfile, accounts['acceptance-a']['tenant_id'])
            tenant.nombre = 'Gobierno de evaluación local'
            tenant.tipo = 'gobierno'
            tenant.plan = 'full'
            tenant.configuracion = {'private_conversation_guide': {'enabled': True, 'guide_id': 'accessible-support-evaluation'}}
            other = db.session.get(TenantProfile, accounts['acceptance-b']['tenant_id'])
            other.nombre = 'Otra organización local'
            other.plan = 'full'
            db.session.commit()

        def snapshot():
            with app.app_context():
                return {'tenants': [(t.id, t.nombre, t.configuracion, t.whatsapp_sender_id) for t in TenantProfile.query.order_by(TenantProfile.id)],
                    'responses': EncRespuesta.query.count(), 'tickets': [m.query.count() for m in (MunicipioTicket, TenantTicket, PymeTicket)],
                    'owners': [(u.id, u.tenant_id, u.tenant_slug, u.password_hash) for u in User.query.order_by(User.id)]}

        before = snapshot()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        runner = frontend / '.vercel/organization-access.browser.mjs'
        runner.parent.mkdir(parents=True, exist_ok=True)
        if runner.exists():
            raise RuntimeError('Refusing to replace existing browser runner')
        shutil.copyfile(Path(__file__).with_name('organization-access.browser.mjs'), runner)
        env = {**os.environ, 'ORGANIZATION_API': f'http://127.0.0.1:{server.server_port}',
            'ORGANIZATION_ACCOUNTS': json.dumps(accounts), 'ORGANIZATION_PASSWORD': password}
        try:
            subprocess.run(['node', str(runner)], cwd=frontend, env=env, check=True, timeout=420)
            assert before == snapshot(), 'Read-only organization flow changed persistent business data'
            print(json.dumps({'storage_unchanged': True, 'external_networks': 'blocked', 'production_accounts_used': False}))
        finally:
            runner.unlink(missing_ok=True)
            server.shutdown(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    main()
