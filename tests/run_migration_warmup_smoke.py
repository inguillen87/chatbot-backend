"""Run the Linux Gunicorn lifecycle fixture with no inherited credentials."""
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='chatboc-warmup-') as directory:
        marker = Path(directory) / 'started'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        child_env = {'VERCEL': '1', 'VERCEL_ENV': 'preview',
                     'WARMUP_SMOKE_MARKER': str(marker), 'GUNICORN_WORKERS': '1'}
        log_path = Path(directory) / 'gunicorn.log'
        with log_path.open('w', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, '-m', 'gunicorn',
                '--config', 'gunicorn.conf.py', '--bind', f'127.0.0.1:{port}',
                'tests.migration_warmup_smoke:application'], cwd=ROOT,
                env=child_env, stdout=log, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 8
                while not marker.exists() and time.monotonic() < deadline:
                    if proc.poll() is not None:
                        raise RuntimeError('gunicorn_stopped_before_warmup')
                    time.sleep(0.05)
                if not marker.exists():
                    raise RuntimeError('worker_did_not_warm_before_first_request')
                with urllib.request.urlopen(f'http://127.0.0.1:{port}/ready', timeout=5) as response:
                    assert response.status == 200
                    assert json.load(response) == {'ok': True, 'method': 'GET'}
                print('PASS: canonical loading began before first HTTP request; real Gunicorn served GET')
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)


if __name__ == '__main__':
    main()
