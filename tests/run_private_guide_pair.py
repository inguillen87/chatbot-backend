"""Actual guide UI/transport + Flask, disposable identities/database, loopback only."""
import argparse
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--frontend', type=Path, required=True)
    frontend = parser.parse_args().frontend.resolve()
    spec = json.loads(Path(__file__).with_name('guide_pair_frontend.json').read_text())
    actual = subprocess.check_output(['git', '-C', str(frontend), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != spec['revision']:
        raise RuntimeError('Paired frontend revision mismatch')
    from tests.profile_acceptance_runtime import prepare_process, create_disposable_app
    prepare_process()
    from database import db
    from models import User, TenantProfile, AuditEvent, EncRespuesta, MunicipioTicket, TenantTicket, PymeTicket
    from services.accessible_support_guide import load_guide, PATH, GUIDE_SHA256
    from utils.auth_helpers import generar_token, auth_session_version
    from werkzeug.serving import make_server
    sockets = [socket.socket() for _ in range(3)]
    for sock in sockets:
        sock.bind(('127.0.0.1', 0))
    ports = [sock.getsockname()[1] for sock in sockets]
    os.environ['CORS_ALLOWED_ORIGINS'] = ','.join(f'http://127.0.0.1:{port}' for port in ports)
    os.environ['CLERK_SUPERADMIN_EMAILS'] = 'root@example.invalid'
    evidence = Path('.qa-artifacts/private-guide-pair').resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='chatboc-guide-pair-') as directory:
        app, accounts, password = create_disposable_app(directory)
        with app.app_context():
            root = User(name='QA platform operator', email='root@example.invalid',
                        rol='super_admin', email_verified=True, acepto_terminos=True)
            root.set_password(password)
            db.session.add(root)
            for slug in ('acceptance-a', 'acceptance-b'):
                tenant = db.session.get(TenantProfile, accounts[slug]['tenant_id'])
                tenant.nombre = 'Institution ' + slug
                tenant.plan = 'full'
                tenant.is_active = True
                tenant.configuracion = {'preserved': {'marker': slug}}
            db.session.flush()
            for user in User.query.all():
                user.entity_token = secrets.token_urlsafe(32)
            db.session.commit()
            root_id = root.id
            identities = {'operator': root_id, 'owner': accounts['acceptance-a']['id'],
                          'foreign': accounts['acceptance-b']['id']}
            actors = {}
            for label, user_id in identities.items():
                user = db.session.get(User, user_id)
                claims = {'sv': auth_session_version(user)}
                if label == 'operator':
                    claims.update(auth_provider='clerk', session_kind='clerk',
                                  clerk_sid='pair-session', clerk_user_id='pair-user',
                                  sid='pair-session', jti='pair-jti')
                actors[label] = {'id': user.id, 'token': generar_token(
                    user.id, user.rol, user.tipo_chat, user.municipio_id, user.pyme_id,
                    extra_claims=claims)}
        guide = load_guide()
        # Paths contain only node IDs and option codes. Content remains served by Flask.
        paths = {'start': []}
        queue = ['start']
        while queue:
            node = queue.pop(0)
            for action in guide['nodes'][node]['actions']:
                target = action['target']
                if target not in paths:
                    paths[target] = paths[node] + [action['code']]
                    queue.append(target)
        if set(paths) != set(guide['nodes']):
            raise AssertionError('Installed guide contains unreachable nodes')

        def snapshot():
            with app.app_context():
                return {'tenants': [(t.id, t.nombre, deepcopy(t.configuracion), t.whatsapp_sender_id)
                                    for t in TenantProfile.query.order_by(TenantProfile.id)],
                        'responses': EncRespuesta.query.count(),
                        'tickets': [m.query.count() for m in (MunicipioTicket, TenantTicket, PymeTicket)],
                        'accounts': [(u.id, u.rol, u.tenant_id, u.tenant_slug, u.password_hash)
                                     for u in User.query.order_by(User.id)]}

        before = snapshot()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        runner_dir = frontend / '.vercel/guide-pair-runtime'
        if runner_dir.exists():
            raise RuntimeError('Refusing to replace an existing paired runtime')
        runner_dir.mkdir(parents=True)
        for source, target in [('guide-pair.browser.mjs', 'runner.mjs'),
                               ('guide-pair-fixture.tsx', 'fixture.tsx'),
                               ('guide-pair-fixture.html', 'index.html')]:
            shutil.copyfile(Path(__file__).with_name(source), runner_dir / target)
        env = {**os.environ, 'GUIDE_PAIR_API': f'http://127.0.0.1:{server.server_port}',
               'GUIDE_PAIR_ACTORS': json.dumps(actors), 'GUIDE_PAIR_PORTS': json.dumps(ports),
               'GUIDE_PAIR_PATHS': json.dumps(paths), 'GUIDE_PAIR_EVIDENCE': str(evidence),
               'GUIDE_PAIR_FRONTEND_SHA': actual, 'VITE_BACKEND_URL': '/api',
               'VITE_API_URL': '/api', 'VITE_USE_LOCAL_API_PROXY': 'true',
               'VITE_BACKEND_BOOTSTRAP_GATE_ENABLED': 'true'}
        for sock in sockets:
            sock.close()
        try:
            subprocess.run(['node', str(runner_dir / 'runner.mjs')], cwd=frontend,
                           env=env, check=True, timeout=480)
            after = snapshot()
            assert before['accounts'] == after['accounts'], 'Account properties changed'
            assert before['tickets'] == after['tickets'] and before['responses'] == after['responses']
            for prior, current in zip(before['tenants'], after['tenants']):
                assert prior[:2] == current[:2] and prior[3] == current[3]
                config = deepcopy(current[2])
                if current[0] == accounts['acceptance-a']['tenant_id']:
                    assert config.pop('private_conversation_guide') == {
                        'enabled': False, 'guide_id': 'accessible-support-evaluation'}
                    assert config.pop('private_conversation_guide_version') == 6
                assert config == prior[2], 'Unrelated tenant configuration changed'
            with app.app_context():
                rows = AuditEvent.query.filter(AuditEvent.event_type.like('conversation_guide.%')).order_by(AuditEvent.id).all()
                assert len(rows) == 6
                assert [row.details['version'] for row in rows] == list(range(1, 7))
                assert all(row.actor_user_id == root_id and row.tenant_id == accounts['acceptance-a']['tenant_id'] for row in rows)
            assert sha256(PATH.read_bytes()).hexdigest() == GUIDE_SHA256
            proof = {'frontendSha': actual, 'backendSha': subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], text=True).strip(), 'auditEvents': 6,
                'finalVersion': 6, 'finalEnabled': False, 'otherTenantUnchanged': True,
                'accountsUnchanged': True, 'ticketsAndSurveyResponsesUnchanged': True,
                'sourceBytesUnchanged': True, 'productionAccountsUsed': False,
                'externalIdentityProviderTested': False, 'httpPayloadsMocked': False}
            (evidence / 'persistence.json').write_text(json.dumps(proof, indent=2), encoding='utf-8')
            print(json.dumps(proof))
        finally:
            server.shutdown(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()
            shutil.rmtree(runner_dir)

if __name__ == '__main__':
    main()
