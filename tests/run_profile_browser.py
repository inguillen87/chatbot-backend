"""Synthetic test accounts in a disposable local app; no customer credentials.
Runs the existing login form against the real local Flask application.
"""
import argparse
from pathlib import Path
import json
import os
import subprocess
import tempfile
import threading
from tests.profile_acceptance_runtime import prepare_process, create_disposable_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', required=True, type=Path)
    root = parser.parse_args().frontend.resolve()
    assert (root / 'tests/profile-http.browser.mjs').is_file()
    prepare_process()
    from werkzeug.serving import make_server
    with tempfile.TemporaryDirectory(prefix='chatboc-profile-browser-') as directory:
        app, accounts, synthetic_password = create_disposable_app(directory)
        # Synthetic Full organization only; this runner is isolated and never connects customer databases.
        from database import db
        from models import TenantProfile
        with app.app_context():
            db.session.get(TenantProfile, accounts['acceptance-a']['tenant_id']).plan = 'full'
            db.session.commit()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = dict(os.environ)
        env['PROFILE_BACKEND_ORIGIN'] = f'http://127.0.0.1:{server.server_port}'
        env['PROFILE_TEST_ACCOUNTS'] = json.dumps(accounts)
        env['PROFILE_TEST_PASSWORD'] = synthetic_password
        try:
            result = subprocess.run(['node', 'tests/profile-http.browser.mjs'], cwd=root, env=env)
            return result.returncode
        finally:
            server.shutdown(); thread.join(timeout=5)
            from database import db
            with app.app_context():
                db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
