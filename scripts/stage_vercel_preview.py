"""Stage a traceable, write-fenced Vercel backend Preview; never promote."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

PROJECT = 'prj_mHwkeg5AIgXHPnt7QLRlXur7KBNF'
TEAM = 'team_BV1xuY6BnEzGanfok8GAyjZv'
SCOPE = 'marcelos-projects-c26aa499'
SHA = re.compile(r'[0-9a-f]{40}')
PREVIEW_URL = re.compile(r'https://chatboc-backend-[a-z0-9]+-marcelos-projects-c26aa499\.vercel\.app')


def deployment_arguments(revision: str) -> list[str]:
    if not isinstance(revision, str) or not SHA.fullmatch(revision):
        raise ValueError('exact_revision_required')
    flags = {
        # BACKEND_VERSION is a fallback and cannot override stale platform SHAs.
        'CHATBOC_DEPLOYMENT_REVISION': revision,
        'CUTOVER_WRITER_FENCE_ENABLED': 'true',
        'OUTBOUND_NOTIFICATIONS_ENABLED': 'false',
        'ENABLE_RUNTIME_SCHEMA_SYNC': 'false',
        'ENABLE_RUNTIME_TENANT_INIT': 'false',
        'FLASK_ENABLE_RUNTIME_SCHEMA_SYNC': 'false',
        'FLASK_ENABLE_RUNTIME_TENANT_INIT': 'false',
    }
    result = ['deploy', '--target', 'preview', '--no-wait', '--yes', '--scope', SCOPE]
    for key, value in flags.items():
        result.extend(['--env', f'{key}={value}'])
    return [*result, '--meta', f'chatbocRelease={revision}']


def validate_project(project: dict) -> None:
    if not isinstance(project, dict) or project.get('projectId') != PROJECT or project.get('orgId') != TEAM:
        raise ValueError('unexpected_project_binding')


def validate_inventory(payload: dict) -> int:
    if not isinstance(payload, dict):
        raise ValueError('source_inventory_required')
    files = payload.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('source_inventory_required')
    for item in files:
        name = item if isinstance(item, str) else item.get('path', item.get('file')) if isinstance(item, dict) else None
        if not isinstance(name, str) or not name:
            raise ValueError('source_path_required')
        if name.startswith(('/', '\\')) or ':' in name:
            raise ValueError('source_path_required')
        parts = name.replace('\\', '/').split('/')
        forbidden = {'.git', '.vercel', 'node_modules', 'test-evidence', '.venv', 'venv', '.codex-venv'}
        if (any(part in forbidden or part == '..' for part in parts)
                or any(part.startswith('.env') and part not in {'.env.example', '.env.sample'} for part in parts)
                or name.lower().endswith(('.sqlite', '.sqlite3', '.db', '.pem', '.key'))):
            raise ValueError('private_or_unreviewed_artifact_in_upload')
    framework = payload.get('framework')
    if not isinstance(framework, dict) or framework.get('slug') != 'container':
        raise ValueError('container_runtime_required')
    return len(files)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--deploy', action='store_true', help='Create one Preview; default only validates/plans.')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    receipt = {'contract_version': 'chatboc.fenced_preview_stage.v1', 'deployed': False, 'promotion_authorized': False}
    try:
        command = deployment_arguments(args.revision)
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
        if head != args.revision:
            raise ValueError('worktree_revision_mismatch')
        if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=root, text=True).strip():
            raise ValueError('clean_worktree_required')
        remote = subprocess.check_output(['git', 'config', '--get', 'remote.origin.url'], cwd=root, text=True).strip()
        if remote not in {'https://github.com/inguillen87/chatbot-backend', 'https://github.com/inguillen87/chatbot-backend.git', 'git@github.com:inguillen87/chatbot-backend.git'}:
            raise ValueError('unexpected_source_repository')
        validate_project(json.loads((root / '.vercel/project.json').read_text(encoding='utf-8-sig')))
        cli = shutil.which('vercel.cmd' if os.name == 'nt' else 'vercel')
        if not cli:
            raise ValueError('authenticated_vercel_cli_required')
        inventory = subprocess.run([cli, 'deploy', '--dry', '--scope', SCOPE], cwd=root,
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
        if inventory.returncode:
            raise ValueError('source_inventory_failed')
        count = validate_inventory(json.loads(inventory.stdout))
        receipt.update(revision=head, project_id=PROJECT, target='preview', source_files=count,
            writer_fence=True, runtime_schema_sync=False, command=command)
        if args.deploy:
            # Recheck immediately before upload. Do not stage a concurrently edited tree.
            if subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip() != head or subprocess.check_output(
                    ['git', 'status', '--porcelain', '--untracked-files=normal'], cwd=root, text=True).strip():
                raise ValueError('worktree_changed_before_upload')
            result = subprocess.run([cli, *command], cwd=root, capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=180)
            urls = set(PREVIEW_URL.findall(result.stdout + '\n' + result.stderr))
            if result.returncode or len(urls) != 1:
                raise ValueError('deployment_unconfirmed_inspect_before_retry')
            receipt.update(deployed=True, url=urls.pop(), ready_verified=False)
        code = 0
    except ValueError as error:
        # Error messages above are fixed codes, never CLI output or credential values.
        known = {'unexpected_source_repository','exact_revision_required','unexpected_project_binding','source_inventory_required','source_path_required',
            'private_or_unreviewed_artifact_in_upload','container_runtime_required','worktree_revision_mismatch',
            'clean_worktree_required','authenticated_vercel_cli_required','source_inventory_failed',
            'worktree_changed_before_upload','deployment_unconfirmed_inspect_before_retry'}
        receipt['error'] = str(error) if str(error) in known else 'invalid_release_metadata'
        code = 1
    except (OSError, subprocess.SubprocessError):
        receipt['error'] = 'release_tool_failure_inspect_before_retry'
        code = 1
    encoded = json.dumps(receipt, indent=2) + '\n'
    if args.output:
        args.output.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
