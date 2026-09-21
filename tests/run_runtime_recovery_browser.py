"""Pair the pinned real frontend recovery component with full disposable Flask."""
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
    expected = '66ab05294d25bf3dfffe436cd98a75bf0c8c92f3'
    revision = subprocess.check_output(['git', '-C', str(frontend), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != expected:
        raise RuntimeError('Acceptance requires the reviewed frontend revision')
    from tests.profile_acceptance_runtime import prepare_process
    prepare_process()
    from tests.runtime_recovery_http_acceptance import RecoveryApplicationServer
    runtime = RecoveryApplicationServer()
    destination = frontend / '.vercel' / 'runtime-recovery-http.browser.mjs'
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        runtime.close()
        raise RuntimeError('Do not overwrite a previous acceptance runner')
    shutil.copyfile(Path(__file__).with_name('runtime-recovery-http.browser.mjs'), destination)
    env = {**os.environ, 'CHATBOC_ACCEPTANCE_URL': runtime.origin,
        'CHATBOC_ACCEPTANCE_CONTROL': runtime.control_token,
        'CHATBOC_ACCEPTANCE_EMAIL': runtime.accounts['acceptance-a']['email'],
        'CHATBOC_ACCEPTANCE_PASSWORD': runtime.password,
        'VITE_PROXY_TARGET': runtime.origin, 'VITE_BACKEND_URL': '/api',
        'VITE_BACKEND_BOOTSTRAP_GATE_ENABLED': 'false',
        'VITE_EXPECTED_BACKEND_REVISION': 'a' * 40}
    try:
        result = subprocess.run(['node', str(destination)], cwd=frontend, env=env, timeout=180)
        if result.returncode:
            raise RuntimeError('Paired recovery browser acceptance failed')
        print(json.dumps({'frontend_revision': revision, 'full_flask_app': True,
            'synthetic_accounts_and_database': True, 'external_providers_used': False}))
    finally:
        destination.unlink(missing_ok=True)
        runtime.close()


if __name__ == '__main__':
    main()
