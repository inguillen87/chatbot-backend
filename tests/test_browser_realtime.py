"""Offline source-model SQL admission and HTTP/media contracts; no live voice QA."""
import ast
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import declarative_base, sessionmaker
from services import browser_realtime as voice

Base = declarative_base()
db = SimpleNamespace(Model=Base, **{name: getattr(sa, name) for name in
    ('Column', 'Integer', 'String', 'Boolean', 'ForeignKey', 'DateTime')})
namespace = {'db': db, 'JSONType': sa.JSON(), 'get_local_now': lambda: datetime.now(timezone.utc)}
source = ast.parse((Path(__file__).resolve().parents[1] / 'models.py').read_text(encoding='utf-8'))
for name, columns in [('User', {'id'}), ('TenantProfile', {'id','slug','nombre','tipo','municipio_id','pyme_id','is_active','configuracion'})]:
    original = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == name)
    body = [node for node in original.body if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in columns | {'__tablename__'} for target in node.targets)]
    generated = ast.ClassDef(name=name, bases=[ast.Attribute(value=ast.Name(id='db',ctx=ast.Load()),attr='Model',ctx=ast.Load())], keywords=[], body=body, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[generated],type_ignores=[])), 'voice-source-columns', 'exec'), namespace)
audit = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == 'AuditEvent')
exec(compile(ast.Module(body=[audit], type_ignores=[]), 'voice-source-audit', 'exec'), namespace)
User, Tenant, Audit = (namespace[name] for name in ('User','TenantProfile','AuditEvent'))
NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
DEADLINE = int((NOW + timedelta(days=1)).timestamp())
OFFER = 'v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\nm=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n'


def trial_config(*, cap=2, total=3, deadline=DEADLINE):
    return {'browser_realtime_voice': {'enabled': True,
        'max_sessions_per_hour': cap, 'max_total_sessions': total,
        'trial_expires_at': deadline}}


@pytest.fixture
def store(tmp_path):
    engine = sa.create_engine('sqlite:///' + str(tmp_path / 'voice.sqlite'))
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([User(id=1), User(id=2)])
        session.flush()
        session.add_all([Tenant(id=1, slug='a', nombre='A', tipo='municipio', municipio_id=1,
            is_active=True), Tenant(id=2, slug='b', nombre='B', tipo='municipio', municipio_id=2, is_active=True)])
        session.commit()
    yield Session
    engine.dispose()


def reserve(session, tenant_id=1, actor_id=1, cap=2, clock=NOW, total=3, deadline=DEADLINE,
            revision='published-revision'):
    ledger = voice.VoiceLedger(session, Tenant, Audit, clock=lambda: clock)
    ledger.lock(tenant_id)
    identifier = ledger.reserve(tenant_id, actor_id, revision,
        voice.limits(trial_config(cap=cap, total=total, deadline=deadline)))
    return ledger, identifier


def test_durable_pending_attempt_blocks_another_process_and_tenant_isolation(store):
    with store() as session:
        _, identifier = reserve(session)
    with store() as other_process:
        with pytest.raises(voice.VoiceError, match='previous_session_pending'):
            reserve(other_process)
        other_process.rollback()
        _, other = reserve(other_process, tenant_id=2, actor_id=2)
        assert identifier != other
        assert other_process.query(Audit).count() == 2


def test_unknown_attempt_does_not_expire_away_and_no_transcripts_in_sql(store):
    with store() as session:
        ledger, identifier = reserve(session, clock=NOW-timedelta(days=1))
        ledger.append(1, 1, identifier, 'accepted', {'call_id': 'rtc_fixture'})
    with store() as session:
        with pytest.raises(voice.VoiceError, match='previous_session_pending'):
            reserve(session)
        stored = json.dumps([row.details for row in session.query(Audit)])
        assert OFFER not in stored and 'transcript' not in stored and 'secret' not in stored


def test_accepted_stop_is_idempotent_and_hourly_cap_is_durable(store):
    provider = Mock(return_value={'stopped': True})
    with store() as session:
        ledger, identifier = reserve(session)
        ledger.append(1, 1, identifier, 'accepted', {'call_id': 'rtc_fixture'})
        assert ledger.stop(1, 1, identifier, 'synthetic', provider=provider)['provider_close_accepted']
        assert ledger.stop(1, 1, identifier, 'synthetic', provider=provider)['provider_close_accepted']
        assert provider.call_count == 1
        _, second = reserve(session)
        ledger.append(1, 1, second, 'accepted', {'call_id': 'rtc_second'})
        ledger.stop(1, 1, second, 'synthetic', provider=provider)
    with store() as session:
        with pytest.raises(voice.VoiceError, match='hourly_cap') as error:
            reserve(session)
        assert error.value.status == 429


def test_ambiguous_hangup_never_retries_or_claims_closed(store):
    provider = Mock(side_effect=TimeoutError('must not escape'))
    with store() as session:
        ledger, identifier = reserve(session)
        ledger.append(1, 1, identifier, 'accepted', {'call_id': 'rtc_fixture'})
        for _ in range(2):
            with pytest.raises(voice.VoiceError, match='close_pending'):
                ledger.stop(1, 1, identifier, 'synthetic', provider=provider)
        assert provider.call_count == 1
        assert not session.query(Audit).filter_by(event_type=voice.EVENT+'stopped').first()


def test_other_actor_or_tenant_cannot_close_session(store):
    provider = Mock()
    with store() as session:
        ledger, identifier = reserve(session)
        ledger.append(1, 1, identifier, 'accepted', {'call_id': 'rtc_fixture'})
        for tenant_id, actor_id in ((1,2),(2,1),(2,2)):
            with pytest.raises(voice.VoiceError, match='session_not_found'):
                ledger.stop(tenant_id, actor_id, identifier, 'synthetic', provider=provider)
        assert not provider.called


def test_postgresql_lock_targets_tenant_only():
    statement = sa.select(Tenant).where(Tenant.id == 1).with_for_update(of=Tenant)
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert sql.endswith('FOR UPDATE OF tenant_profile')


@pytest.mark.parametrize('config', [{}, {'browser_realtime_voice': {'enabled':'true','max_sessions_per_hour':1}},
    {'browser_realtime_voice': {'enabled':True}}, {'browser_realtime_voice': {'enabled':True,'max_sessions_per_hour':True}},
    {'browser_realtime_voice': {'enabled':True,'max_sessions_per_hour':4}}])
def test_disabled_by_default_or_unbounded_configuration_cannot_admit(config):
    with pytest.raises(voice.VoiceError): voice.limits(config)


@pytest.mark.parametrize('field,value', [
    ('max_total_sessions', None), ('max_total_sessions', True),
    ('max_total_sessions', 0), ('max_total_sessions', 4), ('max_total_sessions', 1.0),
    ('trial_expires_at', None), ('trial_expires_at', True), ('trial_expires_at', 0),
    ('trial_expires_at', '1790000000'), ('trial_expires_at', 1790000000.0),
    ('trial_expires_at', 253402300800)])
def test_trial_requires_explicit_fixed_integer_deadline_and_total_cap(field, value):
    config = trial_config()
    config['browser_realtime_voice'][field] = value
    with pytest.raises(voice.VoiceError):
        voice.limits(config)


def test_legacy_enabled_config_has_no_grace_period():
    with pytest.raises(voice.VoiceError, match='total_cap_required'):
        voice.limits({'browser_realtime_voice': {'enabled': True, 'max_sessions_per_hour': 1}})


def test_total_admission_survives_terminal_events_actor_revision_and_hour_changes(store):
    with store() as session:
        first, one = reserve(session, clock=NOW-timedelta(days=2))
        first.append(1, 1, one, 'failed', {})
        second, two = reserve(session, actor_id=2, revision='different-revision', clock=NOW-timedelta(days=1))
        second.append(1, 2, two, 'accepted', {'call_id': 'rtc_fixture'})
        second.stop(1, 2, two, 'synthetic', provider=Mock(return_value={'stopped': True}))
        third, three = reserve(session, revision='third-revision')
        third.append(1, 1, three, 'failed', {})
    with store() as next_process:
        # Extending the deadline or moving to tomorrow does not reset totals.
        with pytest.raises(voice.VoiceError, match='total_cap') as error:
            reserve(next_process, actor_id=2, clock=NOW+timedelta(days=2), deadline=DEADLINE+86400*3)
        assert error.value.status == 429
        assert next_process.query(Audit).filter_by(resource_type=voice.CONTRACT,
            event_type=voice.EVENT+'intent').count() == 3
        next_process.rollback()
        _, foreign = reserve(next_process, tenant_id=2, actor_id=2)
        assert foreign not in (one, two, three)


def test_unknown_reservation_is_in_total_snapshot_and_never_expires_away(store):
    with store() as session:
        ledger, _ = reserve(session, clock=NOW-timedelta(days=2))
        assert ledger.admission_snapshot(1, voice.limits(trial_config(total=1))) == {
            'total_sessions_reserved': 1, 'total_sessions_remaining': 0}
        with pytest.raises(voice.VoiceError, match='previous_session_pending'):
            reserve(session)
        assert session.query(Audit).count() == 1


def test_foreign_contract_cannot_consume_quota_or_fake_resolution(store):
    with store() as session:
        session.add(Audit(tenant_id=1, actor_user_id=1, resource_id='foreign',
            resource_type='other-contract', event_type=voice.EVENT+'intent', details={}, created_at=NOW))
        session.commit()
        ledger, identifier = reserve(session, total=1)
        session.add(Audit(tenant_id=1, actor_user_id=1, resource_id=identifier,
            resource_type='other-contract', event_type=voice.EVENT+'stopped', details={}, created_at=NOW))
        session.commit()
        assert ledger.admission_snapshot(1, voice.limits(trial_config(total=1)))['total_sessions_reserved'] == 1
        with pytest.raises(voice.VoiceError, match='previous_session_pending'):
            reserve(session, total=1)


def test_deadline_equal_now_denies_and_expiry_during_admission_queries_reserves_nothing(store):
    with store() as session:
        with pytest.raises(voice.VoiceError, match='trial_expired'):
            reserve(session, deadline=int(NOW.timestamp()))
        assert session.query(Audit).count() == 0
        session.rollback()
        ticks = iter([NOW, NOW, NOW+timedelta(seconds=1)])
        ledger = voice.VoiceLedger(session, Tenant, Audit, clock=lambda: next(ticks))
        ledger.lock(1)
        with pytest.raises(voice.VoiceError, match='trial_expired'):
            ledger.reserve(1, 1, 'published-revision',
                voice.limits(trial_config(deadline=int(NOW.timestamp())+1)))
        assert session.query(Audit).count() == 0


def test_stop_remains_available_after_expiry_without_reconsuming_total(store):
    with store() as session:
        ledger, identifier = reserve(session, total=1)
        ledger.append(1, 1, identifier, 'accepted', {'call_id': 'rtc_fixture'})
        ledger.clock = lambda: NOW+timedelta(days=2)
        provider = Mock(return_value={'stopped': True})
        assert ledger.stop(1, 1, identifier, 'synthetic', provider=provider)['stopped']
        assert ledger.stop(1, 1, identifier, 'synthetic', provider=provider)['stopped']
        assert provider.call_count == 1
        assert ledger.admission_snapshot(1, voice.limits(trial_config(total=1)))['total_sessions_reserved'] == 1


@pytest.mark.parametrize('sdp', ['v=0\r\nm=video 9 X\r\n', OFFER+'m=video 9 X\r\n', 'x', OFFER+'x'*50000, OFFER+'\x00'], ids=['video','mixed','invalid','large','nul'])
def test_invalid_or_video_offer_is_denied(sdp):
    with pytest.raises(voice.VoiceError): voice.validate_offer(sdp)


def state(text='Información pública'):
    return {'revision':'published-revision', 'bundle':{'nodes':{'a':{'id':'a', 'title':'Tema', 'text':text,
        'sources':[], 'links':[], 'actions':[]}}}}


def test_bounded_session_uses_published_corpus_no_tools_or_tracing():
    config = voice.session_config({}, {'OPENAI_REALTIME_MODEL':'gpt-realtime-2.1'}, state())
    assert config['model'] == 'gpt-realtime-2.1'
    assert config['max_output_tokens'] == 512 and config['tools'] == [] and config['tool_choice'] == 'none'
    assert config['tracing'] is None and 'published-revision' in config['instructions']
    assert 'Información pública' in config['instructions']
    with pytest.raises(voice.VoiceError, match='corpus_too_large'):
        voice.session_config({}, {}, state('x' * 65536))


@pytest.mark.parametrize('model, ceiling', [('gpt-realtime-2.1', 65536),
    ('gpt-realtime', 32768), ('gpt-realtime-2', 32768), ('unknown-valid-model', 32768)])
@pytest.mark.parametrize('character', ['x', 'á', '🧭'])
def test_complete_instruction_utf8_boundary_includes_policy_and_revision(model, ceiling, character):
    base = voice.public_instructions(state(''), model=model)
    available = ceiling - len(base.encode('utf-8'))
    width = len(character.encode('utf-8'))
    text = character * (available // width) + 'x' * (available % width)
    accepted = voice.public_instructions(state(text), model=model)
    assert len(accepted.encode('utf-8')) == ceiling
    assert json.loads(accepted.split('Corpus:\n', 1)[1])[0]['text'] == text
    with pytest.raises(voice.VoiceError, match='corpus_too_large') as rejected:
        voice.public_instructions(state(text + 'x'), model=model)
    assert str(rejected.value) == 'browser_voice_corpus_too_large'


def test_current_model_keeps_all_72_public_nodes_in_46976_byte_corpus_without_private_metadata():
    # Synthetic shape and size only: no real tenant documents or nominal data.
    current = state('')
    current['revision'] = 'a' * 64
    nodes = current['bundle']['nodes'] = {}
    for index in range(72):
        identifier = f'topic-{index}'
        nodes[identifier] = {'id': identifier, 'title': f'Tema {index}',
            'text': 'Información pública 🧭 ' * 10,
            'actions': [{'code': '1', 'label': 'Continuar', 'target': f'topic-{(index + 1) % 72}'}],
            'sources': [{'title': 'Synthetic source', 'sha256': 'b' * 64,
                'document_visibility': 'private', 'url': 'file:///private/source.pdf',
                'origin_url': 'private-original-reference', 'local_file_ref': 'C:/private/doc.pdf'}],
            'links': []}
    projection = [{'id': node['id'], 'title': node['title'], 'text': node['text'],
                   'options': node['actions']} for node in nodes.values()]
    current_bytes = len(json.dumps(projection, ensure_ascii=False).encode('utf-8'))
    assert current_bytes < 46976
    nodes['topic-71']['text'] += 'x' * (46976 - current_bytes)
    instructions = voice.session_config({}, {'OPENAI_REALTIME_MODEL': 'gpt-realtime-2.1'}, current)['instructions']
    decoded = json.loads(instructions.split('Corpus:\n', 1)[1])
    assert len(json.dumps(decoded, ensure_ascii=False).encode('utf-8')) == 46976
    assert len(decoded) == 72
    for original, projected in zip(nodes.values(), decoded):
        assert projected == {key: original[key] for key in ('id', 'title', 'text')} | {'options': original['actions']}
    assert 'private-original-reference' not in instructions
    assert 'C:/private/doc.pdf' not in instructions and 'file:///private/source.pdf' not in instructions
    assert 'local_file_ref' not in instructions and 'document_visibility' not in instructions
    assert 'a' * 64 in instructions
    with pytest.raises(voice.VoiceError, match='corpus_too_large'):
        voice.session_config({}, {'OPENAI_REALTIME_MODEL': 'gpt-realtime'}, current)


def test_context_reserve_is_enforced_independently_of_serialized_byte_ceiling(monkeypatch):
    instructions = voice.public_instructions(state('Orientación pública'))
    byte_count = len(instructions.encode('utf-8'))
    assert voice.MAX_INSTRUCTIONS_UTF8_BYTES + voice.CONVERSATION_RESERVE_TOKENS + \
        voice.SESSION_OVERHEAD_RESERVE_TOKENS + voice.MAX_OUTPUT_TOKENS <= 128000
    monkeypatch.setattr(voice, 'VERIFIED_CONTEXT_TOKENS', {'gpt-realtime-2.1':
        byte_count + voice.CONVERSATION_RESERVE_TOKENS + voice.SESSION_OVERHEAD_RESERVE_TOKENS +
        voice.MAX_OUTPUT_TOKENS - 1})
    with pytest.raises(voice.VoiceError, match='corpus_too_large'):
        voice.public_instructions(state('Orientación pública'))


def test_invalid_utf8_in_published_content_is_fixed_error_without_raw_content():
    with pytest.raises(voice.VoiceError, match='corpus_unavailable') as rejected:
        voice.public_instructions(state('private-corruption-marker\ud800'))
    assert str(rejected.value) == 'browser_voice_corpus_unavailable'


def test_provider_request_bounds_success_and_ignores_error_body(monkeypatch):
    monkeypatch.setattr(voice, 'llm_provider_network_allowed', lambda _: True)
    response = Mock(status=201)
    response.getheader.side_effect = lambda key, default=None: {
        'Location':'/v1/realtime/calls/rtc_fixture', 'Content-Encoding':'identity'}.get(key, default)
    response.read.return_value = OFFER.encode()
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(voice.http.client, 'HTTPSConnection', factory)
    answer = voice.provider_request('synthetic-key', 1, sdp=OFFER, config=voice.session_config({}, {}, state()))
    assert answer['call_id'] == 'rtc_fixture'
    assert response.read.call_args.args == (voice.MAX_SDP+1,)
    assert factory.call_args.args == ('api.openai.com',)
    assert connection.request.call_args.args[:2] == ('POST','/v1/realtime/calls')
    response.status = 302
    response.read.reset_mock()
    with pytest.raises(voice.VoiceError, match='provider_rejected'):
        voice.provider_request('synthetic-key', 1, sdp=OFFER, config={})
    assert not response.read.called


@pytest.mark.parametrize('kind', ['invalid', 'oversized', 'encoding', 'utf8', 'read_timeout'])
def test_accepted_provider_response_error_retains_only_validated_private_call_id(monkeypatch, kind):
    monkeypatch.setattr(voice, 'llm_provider_network_allowed', lambda _: True)
    response = Mock(status=201)
    response.getheader.side_effect = lambda key, default=None: {
        'Location': '/v1/realtime/calls/rtc_fixture',
        'Content-Encoding': 'gzip' if kind == 'encoding' else 'identity'}.get(key, default)
    response.read.return_value = {'invalid': b'not-sdp', 'oversized': b'x' * (voice.MAX_SDP+1),
                                 'utf8': b'\xff'}.get(kind, OFFER.encode())
    if kind == 'read_timeout': response.read.side_effect = TimeoutError('must not escape')
    connection = Mock(); connection.getresponse.return_value = response
    monkeypatch.setattr(voice.http.client, 'HTTPSConnection', Mock(return_value=connection))
    with pytest.raises(voice.AcceptedCallError) as failure:
        voice.provider_request('synthetic-key', 1, sdp=OFFER, config={})
    assert failure.value.call_id == 'rtc_fixture'
    assert 'rtc_fixture' not in str(failure.value) and 'synthetic-key' not in str(failure.value)
    assert connection.request.call_count == 1 and connection.close.call_count == 1
    if kind == 'encoding': assert not response.read.called


def test_unknown_provider_location_never_becomes_an_accepted_call(monkeypatch):
    monkeypatch.setattr(voice, 'llm_provider_network_allowed', lambda _: True)
    response = Mock(status=201); response.getheader.return_value = 'https://foreign.test/rtc_fixture'
    connection = Mock(); connection.getresponse.return_value = response
    monkeypatch.setattr(voice.http.client, 'HTTPSConnection', Mock(return_value=connection))
    with pytest.raises(voice.VoiceError) as failure:
        voice.provider_request('synthetic-key', 1, sdp=OFFER, config={})
    assert not isinstance(failure.value, voice.AcceptedCallError) and not response.read.called


def test_existing_environment_key_is_used_without_overriding_explicit_disabled_config(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', ' synthetic-environment-key ')
    assert voice.resolve_provider_key({}) == 'synthetic-environment-key'
    assert voice.resolve_provider_key({'OPENAI_API_KEY':'synthetic-injected-key'}) == 'synthetic-injected-key'
    assert voice.resolve_provider_key({'OPENAI_API_KEY':None}) is None
    assert voice.resolve_provider_key({'OPENAI_API_KEY':''}) is None
    monkeypatch.delenv('OPENAI_API_KEY')
    assert voice.resolve_provider_key({}) is None


def test_test_network_fence_blocks_before_any_connection_is_constructed(monkeypatch):
    from flask import Flask
    factory=Mock()
    monkeypatch.setattr(voice.http.client, 'HTTPSConnection', factory)
    app=Flask('voice-network-denial');app.config.update(TESTING=True,OPENAI_ALLOW_NETWORK_IN_TESTS=False)
    monkeypatch.setenv('OPENAI_ALLOW_NETWORK_IN_TESTS','false')
    with app.app_context(),pytest.raises(voice.VoiceError,match='test_network_disabled'):
        voice.provider_request('synthetic',1,sdp=OFFER,config={})
    assert not factory.called


@pytest.mark.parametrize('location', ['https://evil.test/v1/realtime/calls/rtc_x','/v1/realtime/calls/rtc_x?token=x','/v1/realtime/calls/a',''])
def test_provider_location_cannot_select_a_foreign_host_or_path(location):
    with pytest.raises(voice.VoiceError): voice._call_id(location)


def test_explicit_auth_guard_rejects_widget_demo_missing_and_retired_identity(monkeypatch):
    from flask import Flask, g
    from routes import browser_realtime as routes
    actor = SimpleNamespace(id=1)
    monkeypatch.setattr(routes, 'is_user_auth_disabled', lambda _: False)
    monkeypatch.setattr(routes, 'request_auth_session_active', lambda _: True)
    with Flask('voice-auth').test_request_context():
        for kind in ('widget','demo'):
            g.token_payload={'session_kind':kind};g.current_user=actor;g.auth_credential_source='token'
            with pytest.raises(voice.VoiceError): routes._actor()
        g.token_payload={};g.current_user=None
        with pytest.raises(voice.VoiceError): routes._actor()
        g.current_user=actor
        assert routes._actor() is actor
        monkeypatch.setattr(routes, 'request_auth_session_active', lambda _: False)
        with pytest.raises(voice.VoiceError): routes._actor()


def test_owner_guard_rejects_employee_and_foreign_owner(monkeypatch):
    from routes import browser_realtime as routes
    monkeypatch.setattr(routes, 'can_manage_tenant_control_plane', lambda actor,tenant,**kw: True)
    monkeypatch.setattr(routes, 'is_authorized_superadmin_user', lambda actor: False)
    tenant=SimpleNamespace(municipio_id=1,pyme_id=None)
    routes._authorize(SimpleNamespace(id=1),tenant)
    with pytest.raises(voice.VoiceError): routes._authorize(SimpleNamespace(id=2),tenant)


@pytest.fixture
def route_client(store, monkeypatch):
    """Actual route handlers with source-model SQL; auth lineage itself tested above."""
    from flask import Flask
    from routes import browser_realtime as routes
    from utils import auth_helpers as auth
    session = store()
    tenant=session.get(Tenant,1)
    tenant.configuracion=trial_config()
    session.commit()
    app=Flask('voice-http');app.config['OPENAI_API_KEY']='synthetic-key'
    monkeypatch.setattr(routes,'db',SimpleNamespace(session=session))
    monkeypatch.setattr(routes,'TenantProfile',Tenant);monkeypatch.setattr(routes,'User',User);monkeypatch.setattr(routes,'AuditEvent',Audit)
    monkeypatch.setattr(auth,'obtener_token',lambda:'synthetic.jwt.fixture')
    monkeypatch.setattr(auth,'_decode_token_payload',lambda token:{'user_id':1,'asid':'fixture-session'})
    monkeypatch.setattr(auth,'_is_jwt_token',lambda token:True)
    monkeypatch.setattr(auth,'user_from_token',lambda token:session.get(User,1))
    monkeypatch.setattr(auth,'_resolve_owner_user',lambda user:user)
    monkeypatch.setattr(auth,'user_tenant_auth_allowed',lambda user:True)
    monkeypatch.setattr(auth,'current_user',SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr(auth,'get_or_create_anon_id',lambda:'fixture-anon')
    monkeypatch.setattr(routes,'can_manage_tenant_control_plane',lambda actor,tenant,**kw: actor.id==tenant.municipio_id and tenant.is_active)
    monkeypatch.setattr(routes,'is_authorized_superadmin_user',lambda actor:False)
    monkeypatch.setattr(routes,'is_user_auth_disabled',lambda actor:False)
    monkeypatch.setattr(routes,'auth_session_version',lambda actor:1)
    monkeypatch.setattr(routes,'request_auth_session_active',lambda actor:True)
    monkeypatch.setattr(routes,'read_state',lambda tenant,**kw:state())
    original_init = voice.VoiceLedger.__init__
    def init(ledger, *args, clock=None):
        original_init(ledger, *args, clock=clock or (lambda: NOW))
    monkeypatch.setattr(voice.VoiceLedger, '__init__', init)
    app.add_url_rule('/<slug>/sessions','voice-start',routes.start,methods=['POST'])
    app.add_url_rule('/<slug>/capabilities','voice-cap',routes.capabilities,methods=['GET'])
    provider=Mock(return_value={'call_id':'rtc_fixture','sdp':OFFER})
    monkeypatch.setattr(routes,'provider_request',provider)
    yield app.test_client(),session,routes,provider
    session.close()


def test_http_disabled_consent_revision_and_foreign_tenant_deny_before_provider(route_client):
    client,session,routes,provider=route_client
    command={'sdp':OFFER,'revision':'published-revision','consent':True}
    assert client.post('/b/sessions',json=command).status_code==403
    assert client.post('/a/sessions',json={**command,'consent':False}).status_code==400
    assert client.post('/a/sessions',json={**command,'revision':'stale'}).status_code==412
    session.get(Tenant,1).configuracion={};session.commit()
    response=client.get('/a/capabilities')
    assert response.status_code==200 and response.json['enabled'] is False
    assert response.headers['Cache-Control']=='no-store'
    assert client.post('/a/sessions',json=command).status_code==503
    assert not provider.called and session.query(Audit).count()==0


def test_http_success_keeps_key_call_id_and_offer_out_of_browser_response(route_client):
    client,session,routes,provider=route_client
    response=client.post('/a/sessions',json={'sdp':OFFER,'revision':'published-revision','consent':True})
    assert response.status_code==200 and len(response.json['session_id'])==32
    assert 'rtc_fixture' not in response.get_data(as_text=True) and 'synthetic-key' not in response.get_data(as_text=True)
    assert response.json['limits']['hard_duration_limit'] is False
    assert session.query(Audit).count()==2 and provider.call_count==1
    assert client.post('/a/sessions',json={'sdp':OFFER,'revision':'published-revision','consent':True}).status_code==409
    assert provider.call_count==1


@pytest.mark.parametrize('kind', ['legacy', 'expired', 'exhausted'])
def test_http_capabilities_and_start_fail_closed_without_provider_for_trial_limits(route_client, kind):
    client, session, routes, provider = route_client
    tenant = session.get(Tenant, 1)
    if kind == 'legacy':
        tenant.configuracion = {'browser_realtime_voice': {'enabled': True, 'max_sessions_per_hour': 2}}
        expected, status = 'browser_voice_total_cap_required', 503
    elif kind == 'expired':
        tenant.configuracion = trial_config(deadline=int(NOW.timestamp()))
        expected, status = 'browser_voice_trial_expired', 410
    else:
        tenant.configuracion = trial_config(total=1)
        session.commit()
        ledger, identifier = reserve(session, total=1)
        ledger.append(1, 1, identifier, 'failed', {})
        expected, status = 'browser_voice_total_cap', 429
    session.commit()
    before = session.query(Audit).count()
    capability = client.get('/a/capabilities')
    assert capability.status_code == 200 and capability.json['enabled'] is False
    assert capability.json['reason_code'] == expected
    assert capability.headers['Cache-Control'] == 'no-store'
    if kind != 'legacy':
        assert capability.json['admission']['total_sessions_reserved'] == (1 if kind == 'exhausted' else 0)
        assert capability.json['limits']['trial_expires_at'] == tenant.configuracion['browser_realtime_voice']['trial_expires_at']
        assert capability.json['ui']['disabled'] != voice.UI['disabled']
    response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
    assert response.status_code == status and response.json['reason_code'] == expected
    assert not provider.called and session.query(Audit).count() == before
    assert voice.UI['disabled'] == 'Esta prueba de voz todavía no está habilitada. El chat por texto sigue disponible.'


@pytest.mark.parametrize('close_success', [True, False])
def test_expiry_during_provider_closes_once_and_never_returns_sdp(route_client, close_success):
    client, session, routes, provider = route_client
    deadline = int(NOW.timestamp())+1
    session.get(Tenant, 1).configuracion = trial_config(total=1, deadline=deadline)
    session.commit()
    original_clock = routes.VoiceLedger.__init__
    def init(ledger, *args, **kwargs):
        original_clock(ledger, *args, **kwargs)
        ledger.clock = lambda: current[0]
    current = [NOW]
    # This clock changes only when the single synthetic provider call returns.
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(routes.VoiceLedger, '__init__', init)
        def request(key, actor_id, **kwargs):
            if kwargs.get('call_id'):
                if not close_success:
                    raise voice.VoiceError('browser_voice_provider_unknown')
                return {'stopped': True}
            current[0] = NOW+timedelta(seconds=1)
            return {'call_id': 'rtc_fixture', 'sdp': OFFER}
        provider.side_effect = request
        response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
        assert response.status_code == (410 if close_success else 409)
        assert response.json['reason_code'] == ('browser_voice_trial_expired' if close_success else 'browser_voice_close_pending')
        assert 'sdp' not in response.json and 'rtc_fixture' not in response.get_data(as_text=True)
        assert provider.call_count == 2
        rows = session.query(Audit).order_by(Audit.id).all()
        assert [row.event_type for row in rows] == [voice.EVENT+kind for kind in
            (['intent', 'accepted', 'stop_intent', 'stopped'] if close_success else ['intent', 'accepted', 'stop_intent'])]
        again = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
        assert again.status_code == 410 and provider.call_count == 2


def test_one_session_total_is_not_reconsumed_by_post_provider_revalidation(route_client):
    client, session, routes, provider = route_client
    session.get(Tenant, 1).configuracion = trial_config(total=1)
    session.commit()
    response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
    assert response.status_code == 200 and provider.call_count == 1
    assert response.json['limits']['max_total_sessions'] == 1
    assert session.query(Audit).filter_by(event_type=voice.EVENT+'intent').count() == 1


def test_deadline_crossing_durable_reservation_never_calls_provider(route_client, monkeypatch):
    client, session, routes, provider = route_client
    session.get(Tenant, 1).configuracion = trial_config(deadline=int(NOW.timestamp())+1)
    session.commit()
    original_init = voice.VoiceLedger.__init__
    ticks = iter([NOW, NOW, NOW, NOW, NOW+timedelta(seconds=1), NOW+timedelta(seconds=1)])
    def init(ledger, *args, **kwargs):
        original_init(ledger, *args, **kwargs)
        ledger.clock = lambda: next(ticks)
    monkeypatch.setattr(voice.VoiceLedger, '__init__', init)
    response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
    assert response.status_code == 410 and not provider.called
    assert [row.event_type for row in session.query(Audit).order_by(Audit.id)] == [
        voice.EVENT+'intent', voice.EVENT+'failed']


@pytest.mark.parametrize('changed', [
    {'max_total_sessions': 2}, {'max_sessions_per_hour': 1},
    {'trial_expires_at': DEADLINE+3600}])
def test_trial_terms_changed_during_provider_are_closed_once(route_client, changed):
    client, session, routes, provider = route_client
    def request(key, actor_id, **kwargs):
        if kwargs.get('call_id'):
            return {'stopped': True}
        config = trial_config()
        config['browser_realtime_voice'].update(changed)
        session.get(Tenant, 1).configuracion = config
        session.commit()
        return {'call_id': 'rtc_fixture', 'sdp': OFFER}
    provider.side_effect = request
    response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
    assert response.status_code == 412 and response.json['reason_code'] == 'browser_voice_state_changed'
    assert 'sdp' not in response.json and provider.call_count == 2
    assert [row.event_type for row in session.query(Audit).order_by(Audit.id)] == [
        voice.EVENT+kind for kind in ('intent','accepted','stop_intent','stopped')]


def test_expiry_during_post_provider_corpus_read_closes_before_response(route_client, monkeypatch):
    client, session, routes, provider = route_client
    session.get(Tenant, 1).configuracion = trial_config(deadline=int(NOW.timestamp())+1)
    session.commit()
    current = [NOW]
    original_init = voice.VoiceLedger.__init__
    def init(ledger, *args, **kwargs):
        original_init(ledger, *args, **kwargs)
        ledger.clock = lambda: current[0]
    monkeypatch.setattr(voice.VoiceLedger, '__init__', init)
    reads = []
    def read(tenant, **kwargs):
        reads.append(True)
        if len(reads) == 2:
            current[0] = NOW+timedelta(seconds=1)
        return state()
    monkeypatch.setattr(routes, 'read_state', read)
    def request(key, actor_id, **kwargs):
        return {'stopped': True} if kwargs.get('call_id') else {'call_id': 'rtc_fixture', 'sdp': OFFER}
    provider.side_effect = request
    response = client.post('/a/sessions', json={'sdp': OFFER, 'revision': 'published-revision', 'consent': True})
    assert response.status_code == 410 and 'sdp' not in response.json
    assert provider.call_count == 2 and len(reads) == 2
    assert session.query(Audit).filter_by(event_type=voice.EVENT+'stopped').count() == 1


def test_capabilities_denies_pending_or_hourly_limit_without_new_reservation(route_client):
    client, session, routes, provider = route_client
    ledger, identifier = reserve(session, cap=1)
    capability = client.get('/a/capabilities')
    assert capability.json['enabled'] is False
    assert capability.json['reason_code'] == 'browser_voice_previous_session_pending'
    assert capability.json['ui']['disabled'] == voice.UI['pending']
    ledger.append(1, 1, identifier, 'failed', {})
    session.get(Tenant, 1).configuracion = trial_config(cap=1)
    session.commit()
    capability = client.get('/a/capabilities')
    assert capability.json['enabled'] is False
    assert capability.json['reason_code'] == 'browser_voice_hourly_cap'
    assert capability.json['admission'] == {'total_sessions_reserved': 1, 'total_sessions_remaining': 2}
    assert not provider.called and session.query(Audit).count() == 2


def test_capabilities_ledger_failure_is_fixed_disabled_response_without_calls(route_client, monkeypatch):
    client, session, routes, provider = route_client
    monkeypatch.setattr(voice.VoiceLedger, 'admission_snapshot',
        Mock(side_effect=sa.exc.SQLAlchemyError('private-database-marker')))
    response = client.get('/a/capabilities')
    assert response.status_code == 503
    assert response.json['reason_code'] == 'browser_voice_ledger_unavailable'
    assert 'private-database-marker' not in response.get_data(as_text=True)
    assert not provider.called and session.query(Audit).count() == 0


def test_provider_timeout_has_durable_intent_and_cannot_issue_a_second_call(route_client):
    client,session,routes,provider=route_client
    provider.side_effect=voice.VoiceError('browser_voice_provider_unknown')
    command={'sdp':OFFER,'revision':'published-revision','consent':True}
    assert client.post('/a/sessions',json=command).status_code==503
    assert client.post('/a/sessions',json=command).status_code==409
    assert provider.call_count==1 and session.query(Audit).count()==1


@pytest.mark.parametrize('closure_success', [True, False])
def test_accepted_unusable_sdp_is_closed_once_with_durable_private_receipts(route_client, closure_success):
    client, session, routes, provider = route_client
    def request(key, actor_id, **kwargs):
        if kwargs.get('call_id'):
            assert kwargs['call_id'] == 'rtc_fixture'
            if not closure_success: raise voice.VoiceError('browser_voice_provider_unknown')
            return {'stopped': True}
        raise voice.AcceptedCallError('browser_voice_provider_response_invalid', 'rtc_fixture')
    provider.side_effect = request
    command = {'sdp': OFFER, 'revision': 'published-revision', 'consent': True}
    response = client.post('/a/sessions', json=command)
    assert response.status_code == (503 if closure_success else 409)
    assert 'sdp' not in response.json and 'rtc_fixture' not in response.get_data(as_text=True)
    assert provider.call_count == 2
    assert 'call_id' not in provider.call_args_list[0].kwargs
    assert provider.call_args_list[1].kwargs == {'call_id': 'rtc_fixture'}
    rows = session.query(Audit).order_by(Audit.id).all()
    assert [row.event_type for row in rows] == [voice.EVENT + name for name in
        (['intent', 'accepted', 'stop_intent', 'stopped'] if closure_success else ['intent', 'accepted', 'stop_intent'])]
    assert OFFER not in json.dumps([row.details for row in rows])
    if not closure_success:
        assert client.post('/a/sessions', json=command).status_code == 409
        with pytest.raises(voice.VoiceError, match='close_pending'):
            voice.VoiceLedger(session, Tenant, Audit).stop(1, 1, rows[0].resource_id, 'synthetic', provider=provider)
        assert provider.call_count == 2


def test_retired_session_after_provider_ack_is_closed_before_delivering_sdp(route_client,monkeypatch):
    client,session,routes,provider=route_client
    closure=Mock(return_value={'provider_close_accepted':True})
    monkeypatch.setattr(voice.VoiceLedger,'stop',closure)
    monkeypatch.setattr(routes,'request_auth_session_active',Mock(side_effect=[True,False]))
    response=client.post('/a/sessions',json={'sdp':OFFER,'revision':'published-revision','consent':True})
    assert response.status_code==412 and 'sdp' not in response.json
    assert provider.call_count==1 and closure.call_count==1


def test_audit_commit_failure_never_calls_provider(route_client,monkeypatch):
    client,session,routes,provider=route_client
    monkeypatch.setattr(session,'commit',Mock(side_effect=sa.exc.SQLAlchemyError('synthetic')))
    response=client.post('/a/sessions',json={'sdp':OFFER,'revision':'published-revision','consent':True})
    assert response.status_code==503 and not provider.called
    assert session.query(Audit).count()==0


def test_tenant_lock_refreshes_configuration_already_loaded_in_identity_map(store):
    with store() as first:
        cached=first.get(Tenant,1)
        cached.configuracion={'browser_realtime_voice':{'enabled':True,'max_sessions_per_hour':2}}
        first.commit()
        with store() as second:
            second.get(Tenant,1).configuracion={'browser_realtime_voice':{'enabled':False}}
            second.commit()
        assert cached.configuracion['browser_realtime_voice']['enabled'] is True
        ledger=voice.VoiceLedger(first,Tenant,Audit)
        refreshed=ledger.lock(1)
        assert refreshed.configuracion['browser_realtime_voice']['enabled'] is False
        with pytest.raises(voice.VoiceError):voice.limits(refreshed.configuracion)


def test_real_decorator_rejects_absent_widget_and_demo_credentials_before_handler(route_client,monkeypatch):
    from utils import auth_helpers as auth
    client,session,routes,provider=route_client
    command={'sdp':OFFER,'revision':'published-revision','consent':True}
    monkeypatch.setattr(auth,'obtener_token',lambda:None)
    assert client.post('/a/sessions',json=command).status_code==401
    monkeypatch.setattr(auth,'obtener_token',lambda:'synthetic.jwt.fixture')
    for kind in ('widget','demo'):
        monkeypatch.setattr(auth,'_decode_token_payload',lambda token,kind=kind:{'user_id':1,'session_kind':kind})
        assert client.post('/a/sessions',json=command).status_code==403
    assert not provider.called and session.query(Audit).count()==0


@pytest.mark.parametrize('closure_success', [True,False])
@pytest.mark.parametrize('unusable_sdp', [True,False])
def test_failed_ack_commit_closes_known_call_once_never_recreates_or_returns_sdp(route_client,monkeypatch,closure_success,unusable_sdp):
    client,session,routes,provider=route_client
    original_commit=session.commit
    commits=[]
    def commit():
        commits.append(True)
        if len(commits)==2:raise sa.exc.SQLAlchemyError('ack fixture')
        original_commit()
    monkeypatch.setattr(session,'commit',commit)
    def request(key,actor_id,**kwargs):
        if kwargs.get('call_id'):
            assert kwargs['call_id']=='rtc_fixture'
            if not closure_success:raise voice.VoiceError('browser_voice_provider_unknown')
            return {'stopped':True}
        if unusable_sdp:raise voice.AcceptedCallError('browser_voice_provider_response_invalid','rtc_fixture')
        return {'call_id':'rtc_fixture','sdp':OFFER}
    provider.side_effect=request
    response=client.post('/a/sessions',json={'sdp':OFFER,'revision':'published-revision','consent':True})
    assert response.status_code==(503 if closure_success else 409) and 'sdp' not in response.json
    assert provider.call_count==2
    assert 'call_id' not in provider.call_args_list[0].kwargs and provider.call_args_list[1].kwargs=={'call_id':'rtc_fixture'}
    assert session.query(Audit).filter_by(event_type=voice.EVENT+'intent').count()==1
    assert session.query(Audit).filter_by(event_type=voice.EVENT+'stopped').count()==(1 if closure_success else 0)
