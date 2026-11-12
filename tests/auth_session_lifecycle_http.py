"""Real HTTP and SQL Flask-session store on a fresh local synthetic database."""
from tests.profile_acceptance_runtime import prepare_process
if __name__ == '__main__':
    prepare_process()

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import tempfile
from uuid import uuid4
import unittest
from unittest.mock import patch

import jwt
from tests.profile_acceptance_runtime import create_disposable_app


class AuthSessionLifecycleHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ['CORS_ALLOWED_ORIGINS'] = 'https://panel.example.invalid'
        cls.directory = tempfile.TemporaryDirectory(prefix='chatboc-session-lifecycle-')
        cls.app, cls.accounts, cls.password = create_disposable_app(cls.directory.name)
        from models import db
        from utils.migration_managed_session import MigrationManagedSqlAlchemySessionInterface, isolate_retirement_session
        cls.app.session_interface = MigrationManagedSqlAlchemySessionInterface(cls.app, db)
        isolate_retirement_session(cls.app)
        with cls.app.app_context():
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        from models import db
        with cls.app.app_context():
            db.session.remove(); db.engine.dispose()
        cls.directory.cleanup()

    def setUp(self):
        from models import db, AuthSessionRetirement, AuthSessionAudit, AuthSession, AuthProviderSession
        with self.app.app_context():
            for model in (AuthSessionRetirement, AuthSessionAudit, AuthSession, AuthProviderSession):
                model.query.delete()
            db.session.commit()

    def login(self, account='acceptance-a', client=None):
        client = client or self.app.test_client()
        response = client.post('/auth/login', json={'email': self.accounts[account]['email'], 'password': self.password})
        self.assertEqual(response.status_code, 200, response.get_json())
        value = response.get_json()
        self.assertEqual(value['session_retirement']['contract_version'], 'chatboc.session_retirement.v1')
        self.assertEqual(value['session_retirement']['actor_id'], str(self.accounts[account]['id']))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return client, value

    def retire(self, client, value, request_id=None, path='/api/v2/auth/sessions/retire'):
        return client.post(path, json={'proof': value['session_retirement']['proof'], 'request_id': request_id or uuid4().hex})

    def assert_denied(self, response):
        self.assertIn(response.status_code, (401, 403), response.get_json())

    def test_same_actor_new_login_survives_old_retirement_and_all_old_credentials_fail(self):
        client, first = self.login()
        old_session_cookie = client.get_cookie('session').value
        client, second = self.login(client=client)
        new_session_cookie = client.get_cookie('session').value
        self.assertNotEqual(old_session_cookie, new_session_cookie)
        self.assertNotEqual(first['token'], second['token'])
        self.assertNotEqual(first['session_retirement']['lineage_id'], second['session_retirement']['lineage_id'])
        response = self.retire(client, first)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(response.get_json()['local_revoked'])
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(client.get_cookie('session').value, new_session_cookie)
        self.assertEqual(client.get('/auth/me').status_code, 200)
        old_cookie_client = self.app.test_client()
        old_cookie_client.set_cookie('session', old_session_cookie)
        self.assert_denied(old_cookie_client.get('/auth/me'))
        bearer = self.app.test_client()
        self.assert_denied(bearer.get('/auth/me', headers={'Authorization': 'Bearer ' + first['token']}))
        self.assert_denied(bearer.post('/auth/refresh', headers={'Authorization': 'Bearer ' + first['token']}))
        self.assert_denied(bearer.post('/api/v2/auth/refresh', json={'token': first['token']}))
        client.delete_cookie('auth_token')
        self.assertEqual(client.get('/auth/me').status_code, 200)
        self.assertEqual(client.get('/auth/me').get_json()['session_retirement']['lineage_id'], second['session_retirement']['lineage_id'])

    def test_foreign_actor_and_refreshed_child_are_isolated(self):
        first_client, first = self.login()
        second_client, second = self.login('acceptance-b')
        child = first_client.post('/api/v2/auth/refresh', json={'token': first['token']})
        self.assertEqual(child.status_code, 200, child.get_json())
        self.assertEqual(child.get_json()['session_retirement']['lineage_id'], first['session_retirement']['lineage_id'])
        response = self.retire(second_client, first)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(second_client.get('/auth/me').status_code, 200)
        self.assert_denied(self.app.test_client().get('/auth/me', headers={'Authorization': 'Bearer ' + child.get_json()['token']}))

    def test_stale_a_jwt_cookie_cannot_contaminate_live_native_cookie_b(self):
        client, first = self.login()
        client, second = self.login(client=client)
        client.set_cookie('auth_token', first['token'])
        self.assertEqual(self.retire(client, first).status_code, 200)
        profile = client.get('/api/me')
        self.assertEqual(profile.status_code, 200, profile.get_json())
        self.assertEqual(profile.get_json()['session_retirement']['lineage_id'], second['session_retirement']['lineage_id'])
        self.assertNotIn('session_context', profile.get_json())
        self.assert_denied(client.get('/api/me', headers={'Authorization': 'Bearer ' + first['token']}))

    def test_legacy_refresh_keeps_lineage_and_cannot_extend_original_expiration(self):
        client, first = self.login()
        response = client.post('/auth/refresh', headers={'Authorization': 'Bearer ' + first['token']})
        self.assertEqual(response.status_code, 200, response.get_json())
        with self.app.app_context():
            parent = jwt.decode(first['token'], self.app.config['SECRET_KEY'], algorithms=['HS256'])
            child = jwt.decode(response.get_json()['token'], self.app.config['SECRET_KEY'], algorithms=['HS256'])
        self.assertEqual(child['asid'], parent['asid'])
        self.assertNotEqual(child['jti'], parent['jti'])
        self.assertLessEqual(child['exp'], parent['exp'])

    def test_native_login_aliases_emit_verified_retirement_proof(self):
        for path in ('/auth/login', '/api/v2/auth/login', '/login', '/auth/admin/login'):
            with self.subTest(path=path):
                client = self.app.test_client()
                response = client.post(path, json={'email':self.accounts['acceptance-a']['email'], 'password':self.password})
                self.assertEqual(response.status_code, 200, response.get_json())
                value = response.get_json()
                descriptor = value['session_retirement']
                profile = client.get('/api/me', headers={'Authorization':'Bearer ' + value['token']})
                self.assertEqual(profile.status_code, 200, profile.get_json())
                self.assertEqual(profile.get_json()['session_retirement'], descriptor)

    def test_google_verified_identity_adapter_emits_proof_through_actual_http(self):
        from models import db, User
        actor_id = self.accounts['acceptance-a']['id']
        with self.app.app_context():
            actor = db.session.get(User, actor_id); actor.acepto_terminos = True; db.session.commit()
        def trusted_google_identity(*_args, **_kwargs):
            return db.session.get(User, actor_id)
        for path in ('/auth/google-login', '/api/v2/auth/google'):
            with self.subTest(path=path):
                client = self.app.test_client()
                with patch('routes.auth.login_o_crear_usuario', side_effect=trusted_google_identity):
                    response = client.post(path, json={'id_token':'synthetic_offline_provider_fixture'})
                self.assertEqual(response.status_code, 200, response.get_json())
                value = response.get_json()
                self.assertEqual(value['session_retirement']['actor_id'], str(actor_id))
                self.assertEqual(self.retire(client, value).status_code, 200)
                self.assert_denied(self.app.test_client().get('/api/me', headers={'Authorization':'Bearer ' + value['token']}))

    def test_retirement_replay_is_idempotent_and_proof_is_not_an_access_credential(self):
        from models import db, AuthSessionAudit, AuthSessionRetirement
        client, first = self.login()
        request_id = uuid4().hex
        self.assertEqual(self.retire(client, first, request_id).status_code, 200)
        replay = self.retire(client, first, request_id)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.get_json()['status'], 'already_retired')
        self.assertNotIn('Set-Cookie', replay.headers)
        with self.app.app_context():
            self.assertEqual(AuthSessionAudit.query.count(), 1)
            self.assertEqual(AuthSessionRetirement.query.count(), 1)
        self.assert_denied(self.app.test_client().get('/auth/me', headers={'Authorization': 'Bearer ' + first['session_retirement']['proof']}))

    def test_retirement_errors_never_modify_ambient_cookies_or_live_session(self):
        client, value = self.login()
        cookie = client.get_cookie('session').value
        for command in ({}, {'proof': 'invalid', 'request_id': uuid4().hex},
                        {'proof': value['session_retirement']['proof'], 'request_id': 'short'},
                        {'proof': value['session_retirement']['proof'], 'request_id': uuid4().hex, 'actor_id': 2}):
            response = client.post('/api/v2/auth/sessions/retire', json=command)
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('Set-Cookie', response.headers)
            self.assertEqual(client.get_cookie('session').value, cookie)
            self.assertEqual(client.get('/auth/me').status_code, 200)

    def test_audit_failure_rolls_back_revocation_and_receipt(self):
        from models import db, AuthSession, AuthSessionAudit, AuthSessionRetirement
        client, value = self.login()
        original_add = db.session.add
        def guarded_add(record, *args, **kwargs):
            if isinstance(record, AuthSessionAudit):
                raise RuntimeError('synthetic audit rejection')
            return original_add(record, *args, **kwargs)
        with patch.object(db.session, 'add', side_effect=guarded_add):
            response = self.retire(client, value)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('Set-Cookie', response.headers)
        with self.app.app_context():
            self.assertIsNone(db.session.get(AuthSession, value['session_retirement']['lineage_id']).revoked_at)
            self.assertEqual(AuthSessionRetirement.query.count(), 0)
        self.assertEqual(client.get('/auth/me').status_code, 200)

    def test_legacy_unbound_jwt_and_flask_cookie_require_new_login(self):
        user_id = self.accounts['acceptance-a']['id']
        with self.app.app_context():
            legacy = jwt.encode({'user_id': user_id, 'exp': datetime.now(timezone.utc) + timedelta(hours=1)},
                                self.app.config['SECRET_KEY'], algorithm='HS256')
        self.assert_denied(self.app.test_client().get('/auth/me', headers={'Authorization': 'Bearer ' + legacy}))
        cookie_client = self.app.test_client()
        with cookie_client.session_transaction() as saved:
            saved['_user_id'] = str(user_id); saved['_fresh'] = True
        self.assert_denied(cookie_client.get('/auth/me'))

    def test_logout_alias_is_proof_only_and_has_no_cookie_effect(self):
        client, value = self.login()
        response = self.retire(client, value, path='/api/v2/auth/logout')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Set-Cookie', response.headers)

    def test_provider_tombstone_before_exchange_and_late_a_event_preserve_b(self):
        from models import db, User, AuthSession
        from services.auth_session_lifecycle import issue_token, descriptor_for_token, retire_session, provider_session_revoked
        from services.clerk_auth_service import sync_clerk_webhook_event
        from utils.auth_helpers import auth_session_version, user_from_token
        with self.app.app_context():
            actor = db.session.get(User, self.accounts['acceptance-a']['id'])
            actor.accesibilidad = {'auth': {'provider': 'clerk', 'session_version': 1,
                                          'clerk': {'user_id': 'synthetic_clerk_actor'}}}
            db.session.commit()
            original_metadata = deepcopy(actor.accesibilidad)
            def claims(sid):
                return {'user_id': actor.id, 'rol': actor.rol, 'auth_provider': 'clerk', 'session_kind': 'clerk',
                        'sid': sid, 'clerk_sid': sid, 'sv': auth_session_version(actor),
                        'exp': datetime.now(timezone.utc) + timedelta(hours=1)}
            try:
                first = issue_token(claims('synthetic_sid_A'))
                second = issue_token(claims('synthetic_sid_B'))
                version = auth_session_version(actor)
                result = sync_clerk_webhook_event({'type': 'session.ended', 'data':
                    {'id': 'synthetic_sid_A', 'user_id': 'synthetic_clerk_actor'}})
                self.assertEqual(result['status'], 'session_revoked')
                self.assertEqual(auth_session_version(actor), version)
                self.assertIsNone(user_from_token(first))
                self.assertEqual(user_from_token(second).id, actor.id)
                sync_clerk_webhook_event({'type': 'session.revoked', 'data': {'id': 'synthetic_sid_before_exchange'}})
                self.assertTrue(provider_session_revoked('synthetic_sid_before_exchange'))
                with self.assertRaises(ValueError):
                    issue_token(claims('synthetic_sid_before_exchange'))
                db.session.rollback()
            finally:
                actor = db.session.get(User, self.accounts['acceptance-a']['id'])
                actor.accesibilidad = {}
                db.session.commit()

    def test_remember_cookie_cannot_restore_a_retired_lineage(self):
        client, value = self.login()
        with client.session_transaction() as saved:
            saved['_remember'] = 'set'
        self.assertEqual(client.get('/auth/me').status_code, 200)
        remembered = client.get_cookie('remember_token')
        self.assertIsNotNone(remembered)
        response = self.retire(client, value)
        self.assertNotIn('Set-Cookie', response.headers)
        recovered = self.app.test_client()
        recovered.set_cookie('remember_token', remembered.value)
        self.assert_denied(recovered.get('/auth/me'))

    def test_retirement_never_reads_or_saves_ambient_sql_session(self):
        client, value = self.login()
        wrapped = self.app.session_interface.wrapped
        with patch.object(wrapped, 'open_session', side_effect=AssertionError('ambient read')) as read, \
             patch.object(wrapped, 'save_session', side_effect=AssertionError('ambient write')) as write:
            response = self.retire(client, value)
        self.assertEqual(response.status_code, 200, response.get_json())
        read.assert_not_called(); write.assert_not_called()
        self.assertNotIn('Set-Cookie', response.headers)

    def test_selected_widget_credential_cannot_borrow_ambient_panel_authority(self):
        from flask import g, session
        from services.auth_session_lifecycle import cookie_lineage_for_user, request_auth_session_active, descriptor_for_request
        from models import db, User
        client, value = self.login()
        with client.session_transaction() as saved:
            ambient = dict(saved); sid = saved.sid
        with self.app.test_request_context('/auth/me'):
            session.update(ambient); session.sid = sid
            actor_id = self.accounts['acceptance-a']['id']
            self.assertIsNotNone(cookie_lineage_for_user(actor_id))
            g.current_user = db.session.get(User, actor_id)
            g.auth_credential_source = 'token'
            g.token_payload = {'user_id': actor_id, 'session_kind':'widget'}
            self.assertFalse(request_auth_session_active(actor_id))
            self.assertIsNone(descriptor_for_request())

    def test_duplicate_command_keys_and_oversize_body_fail_without_cookie_effect(self):
        client, value = self.login()
        for raw in ('{"proof":"x","proof":"y","request_id":"abcdefghijklmnop"}', 'x' * 2049):
            response = client.post('/api/v2/auth/sessions/retire', data=raw, content_type='application/json')
            self.assertEqual(response.status_code, 400)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(client.get('/auth/me').status_code, 200)

    def test_retirement_after_profile_build_discards_private_response_with_controlled_denial(self):
        import routes.auth as auth_routes
        from services.auth_session_lifecycle import retire_session
        client, value = self.login()
        original = auth_routes.build_profile_payload
        def build_then_retire(*args, **kwargs):
            private = original(*args, **kwargs)
            retire_session(value['session_retirement']['proof'], uuid4().hex)
            return private
        with patch.object(auth_routes, 'build_profile_payload', side_effect=build_then_retire):
            response = client.get('/api/me', headers={'Authorization':'Bearer ' + value['token']})
        self.assertEqual(response.status_code, 401, response.get_json())
        self.assertEqual(response.get_json()['reason_code'], 'auth_session_revoked')
        self.assertNotIn(self.accounts['acceptance-a']['email'], response.get_data(as_text=True))
        self.assertNotIn('token', response.get_json())
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_provider_adapter_retires_only_captured_sid_after_local_commit_and_never_retries(self):
        from types import SimpleNamespace
        from models import db, User, AuthProviderSession
        from services.auth_session_lifecycle import issue_token, descriptor_for_token
        from utils.auth_helpers import user_from_token
        import requests
        ambient, _ = self.login('acceptance-b')
        actor_id = self.accounts['acceptance-a']['id']
        outcomes = [('confirmed', 200, {'id': 'synthetic_retire_A', 'status': 'revoked'}),
                    ('pending', 200, {'id': 'different_sid', 'status': 'revoked'}),
                    ('failed', 403, {}), ('pending', 500, {}), ('pending', None, {})]
        for expected, code, body in outcomes:
            with self.subTest(provider_status=expected, http_status=code):
                with self.app.app_context():
                    actor = db.session.get(User, actor_id)
                    actor.accesibilidad = {'auth': {'provider': 'clerk', 'session_version': 1,
                                          'clerk': {'user_id': 'synthetic_actor'}}}
                    db.session.commit()
                    sid = 'synthetic_retire_A' if code == 200 and body.get('id') == 'synthetic_retire_A' else 'synthetic_retire_' + uuid4().hex
                    def token(provider_sid):
                        return issue_token({'user_id': actor_id, 'rol': actor.rol, 'auth_provider': 'clerk',
                            'session_kind': 'clerk', 'sid': provider_sid, 'clerk_sid': provider_sid, 'sv': 1,
                            'exp': datetime.now(timezone.utc) + timedelta(hours=1)})
                    first = token(sid); second = token('synthetic_retire_B_' + uuid4().hex)
                    value = {'session_retirement': descriptor_for_token(first)}
                def exact_call(url, **kwargs):
                    self.assertEqual(url, 'https://api.clerk.com/v1/sessions/' + sid + '/revoke')
                    self.assertEqual(kwargs['timeout'], (3, 5))
                    self.assertFalse(kwargs['allow_redirects'])
                    with self.app.app_context():
                        self.assertIsNotNone(db.session.get(AuthProviderSession, ('clerk', sid)).revoked_at)
                        self.assertIsNone(user_from_token(first))
                        self.assertEqual(user_from_token(second).id, actor_id)
                    if code is None:
                        raise requests.Timeout('synthetic timeout')
                    return SimpleNamespace(status_code=code, json=lambda: body)
                request_id = uuid4().hex
                with patch.dict('os.environ', {'CLERK_SECRET_KEY': 'sk_test_synthetic_offline'}), \
                     patch('services.clerk_auth_service.requests.post', side_effect=exact_call) as provider:
                    response = self.retire(ambient, value, request_id)
                    replay = self.retire(ambient, value, request_id)
                self.assertEqual(response.status_code, 200, response.get_json())
                self.assertEqual(response.get_json()['provider_revocation']['status'], expected)
                self.assertEqual(replay.get_json()['provider_revocation']['status'], expected)
                self.assertEqual(provider.call_count, 1)
                self.assertNotIn('Set-Cookie', response.headers)
                self.assertEqual(ambient.get('/auth/me').status_code, 200)
        with self.app.app_context():
            actor = db.session.get(User, actor_id); actor.accesibilidad = {}; db.session.commit()

    def test_socket_recipient_authority_and_disconnect_preserve_same_actor_b(self):
        from socket_service import socketio
        from services.auth_session_lifecycle import retire_session
        first_client, first = self.login()
        second_client, second = self.login()
        first_socket = socketio.test_client(self.app, auth={'token': first['token']})
        second_socket = socketio.test_client(self.app, auth={'token': second['token']})
        try:
            self.assertTrue(first_socket.is_connected()); self.assertTrue(second_socket.is_connected())
            room = 'tenant_' + str(self.accounts['acceptance-a']['tenant_id'])
            socketio.emit('authority_probe', {'value': 'before'}, room=room)
            self.assertTrue(first_socket.get_received()); self.assertTrue(second_socket.get_received())
            # Another worker committed the tombstone; this worker has not
            # disconnected A and must independently refuse further delivery.
            with self.app.app_context():
                retire_session(first['session_retirement']['proof'], uuid4().hex)
            self.assertTrue(first_socket.is_connected())
            socketio.emit('authority_probe', {'value': 'after'}, room=room)
            self.assertEqual(first_socket.get_received(), [])
            self.assertTrue(second_socket.get_received())
            response = self.retire(second_client, first)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(first_socket.is_connected())
            self.assertTrue(second_socket.is_connected())
        finally:
            if first_socket.is_connected(): first_socket.disconnect()
            if second_socket.is_connected(): second_socket.disconnect()

    def test_public_widget_socket_cannot_receive_operator_data_or_subscribe_as_owner(self):
        from socket_service import socketio
        from services.auth_session_lifecycle import issue_token
        client, value = self.login()
        actor_id = self.accounts['acceptance-a']['id']
        with self.app.app_context():
            widget = issue_token({'user_id':actor_id, 'session_kind':'widget',
                'exp':datetime.now(timezone.utc) + timedelta(hours=1)})
        panel = socketio.test_client(self.app, auth={'token':value['token']})
        public = socketio.test_client(self.app, auth={'token':widget})
        try:
            self.assertTrue(panel.is_connected()); self.assertTrue(public.is_connected())
            socketio.emit('private_probe', {'value':'private'}, room='tenant_' + str(self.accounts['acceptance-a']['tenant_id']))
            self.assertTrue(panel.get_received())
            self.assertEqual(public.get_received(), [])
            public.emit('subscribe_ticket_updates', {'token':widget, 'tenant_slug':'acceptance-a'})
            self.assertEqual(public.get_received()[0]['args'][0]['error'], 'operator_session_required')
            public.emit('send_chat_message', {'token':widget, 'room':'ticket_1', 'ticket_id':1, 'ticket_type':'municipio', 'message':'blocked'})
            self.assertEqual(public.get_received()[0]['args'][0]['error'], 'operator_session_required')
        finally:
            if panel.is_connected(): panel.disconnect()
            if public.is_connected(): public.disconnect()


if __name__ == '__main__':
    unittest.main(verbosity=2)
