"""Verify a declared frontend/backend Preview pair without writes or credentials.

A READY deployment is not proof that its rewrites reach the intended backend.
This probe verifies HTML build identity, direct backend readiness and the
revision reached THROUGH the frontend. It never authorizes promotion.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from urllib.parse import urlsplit

from scripts.verify_migration_runtime import response_reason, validate_target

CONTRACT = 'chatboc.paired_preview_probe.v1'
SHA = re.compile(r'[0-9a-f]{40}')
FRONTEND_HOST = re.compile(r'chatboc-frontend-[a-z0-9]+-marcelos-projects-c26aa499\.vercel\.app')


def validate_frontend(origin: str, revision: str) -> str:
    url = urlsplit(origin)
    host = url.hostname or ''
    if (not SHA.fullmatch(revision) or url.scheme != 'https' or url.netloc != host
            or url.path not in ('', '/') or url.query or url.fragment or '\\' in origin
            or not (host == 'chatboc-r2-preview.vercel.app' or FRONTEND_HOST.fullmatch(host))):
        raise ValueError('invalid_frontend_preview')
    return 'https://' + host


class BuildRevisionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.revisions = []
        self.invalid = False

    def handle_starttag(self, tag, attrs):
        if tag.lower() != 'meta':
            return
        names = [value for name, value in attrs if name == 'name']
        if 'chatboc-build-revision' not in names:
            return
        values = [value for name, value in attrs if name == 'content']
        if len(names) != 1 or len(values) != 1:
            self.invalid = True
        else:
            self.revisions.append(values[0])


def frontend_matches(body: str, revision: str) -> bool:
    parser = BuildRevisionParser()
    try:
        parser.feed(body)
        parser.close()
    except Exception:
        return False
    return not parser.invalid and parser.revisions == [revision]


def fetch_document(url: str, timeout: float) -> tuple[int, str, str]:
    """OS curl, no curlrc, redirects, cookies or auth headers; bounded response."""
    curl = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
    if not curl:
        return 0, '', ''
    command = [curl, '--disable', '--silent', '--show-error', '--proto', '=https',
        '--max-redirs', '0', '--max-time', str(timeout), '--max-filesize', '65536',
        '--header', 'Cache-Control: no-cache', '--header', 'Accept: application/json, text/html',
        '--write-out', '\n%{http_code}\n%{content_type}', url]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout + 2)
        if result.returncode or len(result.stdout) > 66000:
            return 0, '', ''
        body, status, content_type = result.stdout.rsplit(b'\n', 2)
        return int(status), content_type.decode('ascii').split(';', 1)[0].strip().lower(), body.decode('utf-8')
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0, '', ''


def classify(kind: str, status: int, mime: str, body: str, revision: str) -> str:
    payload = {}
    if mime == 'application/json':
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                payload = parsed
        except (ValueError, TypeError):
            pass
    if kind == 'frontend':
        if status != 200 or mime != 'text/html':
            return 'frontend_document_unavailable'
        return 'ok' if frontend_matches(body, revision) else 'frontend_revision_mismatch'
    if kind == 'access' and status in (401, 403) and mime == 'application/json':
        return 'ok'
    if kind == 'access' and status == 200:
        return 'anonymous_profile_not_rejected'
    return response_reason('/health/ready' if kind == 'readiness' else '/api/version', status, payload, revision)


def verify_pair(frontend: str, backend: str, frontend_revision: str, backend_revision: str,
                *, attempts=3, timeout=10, budget=90, fetch=fetch_document,
                sleep=time.sleep, clock=time.monotonic) -> dict:
    frontend = validate_frontend(frontend, frontend_revision)
    backend = validate_target(backend, backend_revision)
    if type(attempts) is not int or not 1 <= attempts <= 4:
        raise ValueError('invalid_attempts')
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
           for v in (timeout, budget)) or not 1 <= timeout <= 15 or not 15 <= budget <= 120:
        raise ValueError('invalid_time_limits')
    report = {'contract_version': CONTRACT, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'frontend_origin': frontend, 'backend_origin': backend,
        'frontend_revision': frontend_revision, 'backend_revision': backend_revision,
        'version_pair_verified': False, 'first_attempt_ready': False,
        'promotion_authorized': False, 'authenticated_acceptance': False,
        'writes_performed': False, 'samples': []}
    stages = [('frontend', frontend + '/perfil?section=general', frontend_revision),
              ('direct_backend', backend + '/api/version', backend_revision),
              ('readiness', backend + '/health/ready', backend_revision),
              ('proxied_backend', frontend + '/api/version', backend_revision),
              ('access', frontend + '/api/me', backend_revision),
              ('frontend_recheck', frontend + '/perfil?section=general', frontend_revision),
              ('proxied_backend_recheck', frontend + '/api/version', backend_revision)]
    deadline = clock() + budget
    for stage, url, revision in stages:
        kind = 'frontend' if stage.startswith('frontend') else stage
        for attempt in range(1, attempts + 1):
            before = clock()
            remaining = deadline - before
            if remaining < 1:
                report['failure'] = 'verification_deadline_exceeded'
                return report
            try:
                status, mime, body = fetch(url, min(timeout, remaining))
                if type(status) is not int or not isinstance(mime, str) or not isinstance(body, str) or len(body.encode('utf-8')) > 65536:
                    raise ValueError('invalid_transport_result')
            except Exception:
                status, mime, body = 0, '', ''
            reason = classify(kind, status, mime, body, revision)
            report['samples'].append({'stage': stage, 'attempt': attempt,
                'http_status': status, 'duration_ms': round((clock() - before) * 1000), 'reason': reason})
            if clock() > deadline:
                report['failure'] = 'verification_deadline_exceeded'
                return report
            if reason == 'ok':
                break
            if reason != 'bootstrap_retryable' or attempt == attempts or deadline - clock() <= 2:
                report['failure'] = stage + ':' + reason
                return report
            sleep(2)
    report['version_pair_verified'] = True
    report['first_attempt_ready'] = all(row['reason'] == 'ok' for row in report['samples'])
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--frontend-url', required=True)
    parser.add_argument('--backend-url', required=True)
    parser.add_argument('--frontend-revision', required=True)
    parser.add_argument('--backend-revision', required=True)
    parser.add_argument('--require-first-attempt', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = verify_pair(args.frontend_url, args.backend_url, args.frontend_revision, args.backend_revision)
    except ValueError:
        print(json.dumps({'contract_version': CONTRACT, 'error': 'invalid_arguments'}))
        return 2
    encoded = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    if not report['version_pair_verified']:
        return 1
    return 3 if args.require_first_attempt and not report['first_attempt_ready'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
