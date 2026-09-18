"""Bounded, read-only Preview readiness verification; never authorizes cutover.

Checks revision -> PostgreSQL/Redis readiness -> revision again. A healthy
process or an eventually successful retry is not a cold-start SLA certificate.
Only public, credential-free endpoints on the approved Preview hosts are used.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

CONTRACT = 'chatboc.migration_runtime_probe.v1'
SHA = re.compile(r'[0-9a-f]{40}')
IMMUTABLE = re.compile(r'chatboc-backend-[a-z0-9]+-marcelos-projects-c26aa499\.vercel\.app')
PATHS = ('/api/version', '/health/ready', '/api/version')


def validate_target(base: str, revision: str) -> str:
    url = urlsplit(base)
    if not SHA.fullmatch(revision):
        raise ValueError('expected_revision_must_be_exact_sha')
    host = url.hostname or ''
    if (url.scheme != 'https' or url.netloc != host or url.path not in ('', '/')
            or url.query or url.fragment or '\\' in base
            or not (host == 'api-preview.chatboc.ar' or IMMUTABLE.fullmatch(host))):
        raise ValueError('only_approved_preview_origins_are_allowed')
    return 'https://' + host


def fetch_json(url: str, timeout: float) -> tuple[int, dict]:
    """Use the OS curl trust store; ignore curlrc, never redirect or log bodies."""
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not curl:
        return 0, {}
    command = [curl, '--disable', '--silent', '--show-error', '--proto', '=https',
        '--max-redirs', '0', '--max-time', str(timeout), '--max-filesize', '65536',
        '--header', 'Cache-Control: no-cache', '--header', 'Accept: application/json',
        '--write-out', '\n%{http_code}\n%{content_type}', url]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout + 2)
        if result.returncode or len(result.stdout) > 66000:
            return 0, {}
        body, status, content_type = result.stdout.rsplit(b'\n', 2)
        if content_type.split(b';', 1)[0].strip().lower() != b'application/json':
            return int(status), {}
        payload = json.loads(body)
        return int(status), payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0, {}


def response_reason(path: str, status: int, payload: dict, revision: str) -> str:
    if status != 200:
        if (status == 503 and payload.get('contract_version') == 'chatboc.bootstrap.v1'
                and payload.get('reason_code') == 'application_initializing'
                and payload.get('retryable') is True):
            return 'bootstrap_retryable'
        return 'http_or_transport_failure'
    if path == '/api/version':
        return 'ok' if payload.get('backend') == revision else 'revision_mismatch'
    if (payload.get('contract_version') != 'runtime.readiness.v1'
            or payload.get('ready') is not True or payload.get('status') != 'ready'):
        return 'readiness_contract_not_ready'
    components = payload.get('components')
    if not isinstance(components, dict):
        return 'required_components_not_ready'
    for name in ('database', 'redis'):
        item = components.get(name)
        if not isinstance(item, dict) or item.get('status') != 'ok' or item.get('required') is not True:
            return 'required_components_not_ready'
    return 'ok'


def verify(base: str, revision: str, *, attempts: int = 4, timeout: float = 10,
           fetch=fetch_json, sleep=time.sleep) -> dict:
    base = validate_target(base, revision)
    if type(attempts) is not int or not 1 <= attempts <= 6:
        raise ValueError('attempts_out_of_bounds')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 1 <= timeout <= 15:
        raise ValueError('timeout_out_of_bounds')
    results = []
    report = {'contract_version': CONTRACT, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'origin': base, 'expected_revision': revision, 'runtime_ready': False,
        'first_attempt_ready': False, 'cutover_authorized': False, 'samples': results}
    for path in PATHS:
        for attempt in range(1, attempts + 1):
            before = time.monotonic()
            try:
                status, payload = fetch(base + path, timeout)
                if type(status) is not int or not isinstance(payload, dict):
                    status, payload = 0, {}
            except Exception:
                status, payload = 0, {}
            reason = response_reason(path, status, payload, revision)
            results.append({'path': path, 'attempt': attempt, 'http_status': status,
                'duration_ms': round((time.monotonic() - before) * 1000), 'reason': reason})
            if reason == 'ok':
                break
            if reason != 'bootstrap_retryable' or attempt == attempts:
                return report
            sleep(2)
    report['runtime_ready'] = True
    report['first_attempt_ready'] = all(item['reason'] == 'ok' for item in results)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://api-preview.chatboc.ar')
    parser.add_argument('--expected-revision', required=True)
    parser.add_argument('--attempts', type=int, default=4)
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--require-first-attempt', action='store_true')
    args = parser.parse_args()
    try:
        report = verify(args.base_url, args.expected_revision, attempts=args.attempts, timeout=args.timeout)
    except ValueError:
        print(json.dumps({'contract_version': CONTRACT, 'error': 'invalid_arguments'}))
        return 2
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    if not report['runtime_ready']:
        return 1
    return 3 if args.require_first_attempt and not report['first_attempt_ready'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
