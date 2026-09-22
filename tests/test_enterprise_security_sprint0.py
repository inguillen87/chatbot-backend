"""Offline component tests; no production secrets, database, or provider calls."""
import ast
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flask import Blueprint, Flask, current_app, jsonify
from flask_socketio import SocketIO, join_room
from utils.widget_jwks import build_public_widget_jwks
from utils.commerce_realtime import emit_commerce_invalidation

ROOT = Path(__file__).resolve().parents[1]
def public_pem(key):
    return key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
def private_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
def load_handlers(path, names, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding='utf-8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names), 'Requested production handlers must exist'
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)
    return namespace

class PublicJwksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.ec = ec.generate_private_key(ec.SECP256R1())
    def test_symmetric_and_unsupported_algorithms_publish_nothing(self):
        for alg in ['HS256', 'HS384', 'HS512', 'none', 'RS512', 'PS256', '', None]:
            with self.subTest(alg=alg):
                self.assertEqual(build_public_widget_jwks({'WIDGET_JWT_ALG': alg, 'SECRET_KEY': 'private-test-secret', 'WIDGET_JWT_SECRET': 'private-widget-secret'}), {'keys': []})
    def test_public_rsa_and_ec_verify_real_signatures(self):
        for alg, key in [('RS256', self.rsa), ('ES256', self.ec)]:
            with self.subTest(alg=alg):
                result = build_public_widget_jwks({'WIDGET_JWT_ALG': alg, 'WIDGET_JWT_PUBLIC_KEY': public_pem(key), 'WIDGET_JWT_KID': 'rotation-1'})
                jwk = result['keys'][0]
                self.assertEqual(jwk['kid'], 'rotation-1')
                self.assertFalse({'k', 'd', 'p', 'q', 'dp', 'dq', 'qi', 'oth'} & set(jwk))
                token = jwt.encode({'sub': 'fixture'}, private_pem(key), algorithm=alg)
                self.assertEqual(jwt.decode(token, jwt.PyJWK.from_dict(jwk).key, algorithms=[alg])['sub'], 'fixture')
    def test_private_pem_in_public_setting_is_rejected(self):
        for alg, key in [('RS256', self.rsa), ('ES256', self.ec)]:
            with self.subTest(alg=alg):
                self.assertEqual(build_public_widget_jwks({'WIDGET_JWT_ALG': alg, 'WIDGET_JWT_PUBLIC_KEY': private_pem(key)}), {'keys': []})
    def test_no_private_or_secret_fallback(self):
        self.assertEqual(build_public_widget_jwks({'WIDGET_JWT_ALG': 'RS256', 'WIDGET_JWT_PRIVATE_KEY': private_pem(self.rsa), 'WIDGET_JWT_SECRET': 'private'}), {'keys': []})
    def test_invalid_weak_mismatched_or_wrong_curve_keys_fail_closed(self):
        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        curve = ec.generate_private_key(ec.SECP384R1())
        for alg, value in [('RS256', 'not a key'), ('RS256', public_pem(weak)), ('RS256', public_pem(self.ec)), ('ES256', public_pem(self.rsa)), ('ES256', public_pem(curve)), ('RS256', {}), ('RS256', None)]:
            with self.subTest(alg=alg, kind=type(value).__name__):
                self.assertEqual(build_public_widget_jwks({'WIDGET_JWT_ALG': alg, 'WIDGET_JWT_PUBLIC_KEY': value}), {'keys': []})
    def test_serializer_fields_are_allowlisted(self):
        raw = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.rsa.public_key()))
        raw.update({'d': 'must-not-publish', 'k': 'must-not-publish', 'unknown': 'private'})
        with patch('utils.widget_jwks.algorithms.RSAAlgorithm.to_jwk', return_value=json.dumps(raw)):
            result = build_public_widget_jwks({'WIDGET_JWT_ALG': 'RS256', 'WIDGET_JWT_PUBLIC_KEY': public_pem(self.rsa)})
        self.assertEqual(set(result['keys'][0]), {'kty', 'n', 'e', 'use', 'alg', 'kid'})

class JwksHttpTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SECRET_KEY='synthetic-test-secret-not-production', WIDGET_JWT_ALG='HS256')
        auth = Blueprint('auth', __name__, url_prefix='/auth')
        api = Blueprint('auth_api', __name__, url_prefix='/api/auth')
        self.ns = load_handlers('routes/auth.py', {'_widget_jwks_payload', 'widget_jwks', '_sign', '_refresh', '_now'}, {'current_app': current_app, 'jsonify': jsonify, 'auth_bp': auth, 'auth_api_bp': api, 'jwt': jwt, 'datetime': datetime, 'timezone': timezone})
        self.app.register_blueprint(auth)
        self.app.register_blueprint(api)
        self.client = self.app.test_client()
    def test_all_four_http_aliases_never_expose_symmetric_material(self):
        for path in ['/auth/widget/jwks.json', '/api/auth/widget/jwks.json', '/auth/.well-known/jwks.json', '/api/auth/.well-known/jwks.json']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json, {'keys': []})
                self.assertIn('no-store', response.headers['Cache-Control'])
                self.assertEqual(response.headers['Pragma'], 'no-cache')
    def test_public_http_key_verifies_and_is_not_cached(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.app.config.update(WIDGET_JWT_ALG='RS256', WIDGET_JWT_PUBLIC_KEY=public_pem(key))
        response = self.client.get('/api/auth/widget/jwks.json')
        self.assertEqual(response.json['keys'][0]['kty'], 'RSA')
        self.assertIn('no-store', response.headers['Cache-Control'])
    def test_existing_hs256_sign_and_refresh_remain_server_side(self):
        with self.app.app_context():
            token, _ = self.ns['_sign']({'user_id': 7}, 45, 7)
            refreshed = self.ns['_refresh'](token, 45)
            self.assertIsNotNone(refreshed)
            self.assertEqual(jwt.decode(refreshed, self.app.config['SECRET_KEY'], algorithms=['HS256'])['user_id'], 7)

class CommerceInvalidationTests(unittest.TestCase):
    def test_invalid_tenant_ids_never_emit(self):
        for tenant in [None, '', 0, -1, True, 1.5, '1:other', 'tenant_1', '01', ' 1', {}, []]:
            emit = Mock()
            with self.subTest(tenant=tenant):
                self.assertFalse(emit_commerce_invalidation(emit, tenant_id=tenant, resource='payments', event_name='payment_update'))
                emit.assert_not_called()
    def test_event_and_resource_are_allowlisted(self):
        for resource, event in [('secrets', 'payment_update'), ('orders', 'arbitrary'), ('payments', 'market_order_demo')]:
            emit = Mock()
            self.assertFalse(emit_commerce_invalidation(emit, tenant_id=1, resource=resource, event_name=event))
            emit.assert_not_called()
    def test_scoped_payload_contains_no_entity_or_customer_data(self):
        emit = Mock()
        self.assertTrue(emit_commerce_invalidation(emit, tenant_id=7, resource='payments', event_name='payment_update'))
        emit.assert_called_once_with('payment_update', {'contract_version': 'collections.invalidated.v1', 'resource': 'payments', 'reason': 'collection_changed', 'refetch': True}, room='tenant_7')
    def test_payment_handler_uses_scoped_invalidation(self):
        emit = Mock()
        ns = load_handlers('routes/mercadopago_webhook.py', {'_emit_payment_notification'}, {'PedidoConversacional': object, 'socketio': SimpleNamespace(emit=emit), 'emit_commerce_invalidation': emit_commerce_invalidation, 'logging': __import__('logging')})
        ns['_emit_payment_notification'](SimpleNamespace(tenant_id=7, id=99, user_id=3, estado='pagado'))
        self.assertEqual(emit.call_args.kwargs, {'room': 'tenant_7'})
        self.assertNotIn('pedido_id', emit.call_args.args[1])
    def test_webhook_has_no_direct_socket_broadcast_calls(self):
        tree = ast.parse((ROOT / 'routes/mercadopago_webhook.py').read_text(encoding='utf-8'))
        direct = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == 'socketio' and n.func.attr == 'emit']
        self.assertEqual(direct, [])
    def test_transport_failure_is_not_reported_as_success(self):
        with self.assertRaises(RuntimeError):
            emit_commerce_invalidation(Mock(side_effect=RuntimeError('offline')), tenant_id=7, resource='payments', event_name='payment_update')
    def test_two_tenants_and_two_buyers_receive_only_authorized_room_signal(self):
        app = Flask(__name__)
        app.config['SECRET_KEY'] = 'fixture-only'
        sio = SocketIO(app, async_mode='threading')
        # This tests transport isolation using pre-authorized room fixtures,
        # not live authentication or the whole application factory.
        @sio.on('connect')
        def connected(auth):
            rooms = {'operator-a': 'tenant_7', 'operator-b': 'tenant_8'}
            if (auth or {}).get('fixture') in rooms:
                join_room(rooms[auth['fixture']])
        clients = [sio.test_client(app, auth={'fixture': name}) for name in ['operator-a', 'operator-b', 'buyer-a', 'buyer-b']]
        try:
            emit_commerce_invalidation(sio.emit, tenant_id=7, resource='orders', event_name='market_order_demo')
            self.assertEqual(clients[0].get_received()[0]['name'], 'market_order_demo')
            for client in clients[1:]:
                self.assertEqual(client.get_received(), [])
        finally:
            for client in clients:
                client.disconnect()

if __name__ == '__main__':
    unittest.main()
