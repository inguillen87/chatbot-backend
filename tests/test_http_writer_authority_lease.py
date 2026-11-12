"""HTTP admission and real WSGI consumption/close boundaries, without providers."""
from contextlib import contextmanager
from types import SimpleNamespace

from flask import Flask, Response
import pytest
from werkzeug.test import EnvironBuilder

from cutover_writer_fence import cutover_read_only_view, cutover_writer_view, is_cutover_writer_view
import middleware.cutover_writer_fence as gate
from services.global_writer_authority import GlobalWriterAuthorityDecision, GlobalWriterAuthorityTransitionError


def application(monkeypatch, events, *, admitted=True):
    app = Flask(__name__)
    app.config.update(TESTING=True, CUTOVER_WRITER_FENCE_ENABLED=False,
                      CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=True, CUTOVER_RUNTIME_IDENTITY='vercel')
    decision = GlobalWriterAuthorityDecision(True, True, 'runtime_is_global_writer_owner', 2)
    monkeypatch.setattr(gate, 'evaluate_global_writer_authority', lambda config: decision)

    @contextmanager
    def lease(config, *, request_lifetime=False):
        assert request_lifetime is True
        events.append('lease_enter')
        try:
            yield SimpleNamespace(decision=decision if admitted else
                GlobalWriterAuthorityDecision(False, True, 'runtime_globally_fenced', 3))
        finally:
            events.append('lease_release')

    monkeypatch.setattr(gate, 'global_writer_authority_lease', lease)
    gate.register_cutover_writer_fence(app)
    return app


def test_stream_partial_close_keeps_provider_cleanup_and_call_on_close_inside_lease(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        def body():
            try:
                events.append('provider_first_chunk')
                yield b'one'
                events.append('provider_second_chunk')
                yield b'two'
            finally:
                assert 'lease_release' not in events
                events.append('provider_stream_cleanup')
        response = Response(body())
        response.call_on_close(lambda: events.append('provider_close_callback'))
        return response

    response = app.test_client().post('/write', buffered=False)
    assert 'lease_release' not in events
    response.close()
    response.close()
    assert events == ['lease_enter', 'provider_first_chunk', 'provider_stream_cleanup',
                      'provider_close_callback', 'lease_release']


def test_complete_stream_exhaustion_releases_once_after_close_callbacks(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        response = Response(iter((b'one', b'two')))
        response.call_on_close(lambda: events.append('provider_close_callback'))
        return response

    response = app.test_client().post('/write', buffered=False)
    assert response.get_data() == b'onetwo'
    response.close()
    assert events == ['lease_enter', 'provider_close_callback', 'lease_release']


def test_stream_error_releases_after_generator_cleanup_and_close_callbacks(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        def body():
            try:
                yield b'one'
                raise ValueError('bounded_stream_failure')
            finally:
                assert 'lease_release' not in events
                events.append('stream_cleanup')
        response = Response(body())
        response.call_on_close(lambda: events.append('close_callback'))
        return response

    response = app.test_client().post('/write', buffered=False)
    with pytest.raises(ValueError, match='bounded_stream_failure'):
        response.get_data()
    response.close()
    assert events == ['lease_enter', 'stream_cleanup', 'close_callback', 'lease_release']


def test_head_writer_closes_lease_and_callbacks_without_consuming_body(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.get('/write')
    @cutover_writer_view
    def writer():
        def body():
            raise AssertionError('HEAD must not consume the response body')
            yield b'never'
        response = Response(body())
        response.call_on_close(lambda: events.append('close_callback'))
        return response

    response = app.test_client().head('/write')
    assert response.status_code == 200 and response.get_data() == b''
    response.close()
    assert events == ['lease_enter', 'close_callback', 'lease_release']


def test_wsgi_never_consumed_body_releases_on_close(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        response = Response(iter((b'never_consumed',)))
        response.call_on_close(lambda: events.append('close_callback'))
        return response

    environ = EnvironBuilder(path='/write', method='POST').get_environ()
    iterable = app.wsgi_app(environ, lambda status, headers, exc_info=None: None)
    assert events == ['lease_enter']
    iterable.close()
    iterable.close()
    assert events == ['lease_enter', 'close_callback', 'lease_release']


@pytest.mark.parametrize('phase', ['handler', 'after_request'])
def test_wsgi_exception_releases_lease_before_propagation(monkeypatch, phase):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        if phase == 'handler':
            raise ValueError('request_failure')
        return {'ok': True}

    @app.after_request
    def fail(response):
        if phase == 'after_request':
            raise ValueError('request_failure')
        return response

    with pytest.raises(ValueError, match='request_failure'):
        app.test_client().post('/write')
    assert events == ['lease_enter', 'lease_release']


def test_close_callback_exception_still_releases_lease(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.post('/write')
    def writer():
        response = Response(iter((b'one',)))
        def fail():
            assert 'lease_release' not in events
            raise ValueError('callback_failure')
        response.call_on_close(fail)
        return response

    response = app.test_client().post('/write', buffered=False)
    with pytest.raises(ValueError, match='callback_failure'):
        response.close()
    assert events == ['lease_enter', 'lease_release']


def test_changed_authority_between_admission_check_and_lock_prevents_handler(monkeypatch):
    events = []
    app = application(monkeypatch, events, admitted=False)

    @app.post('/write')
    def writer():
        raise AssertionError('a stale pre-check must not admit effects')

    response = app.test_client().post('/write')
    assert response.status_code == 503
    assert response.get_json()['reason_code'] == 'runtime_globally_fenced'
    assert events == ['lease_enter', 'lease_release']


def test_lease_connection_failure_is_redacted_and_prevents_handler(monkeypatch):
    app = application(monkeypatch, [])

    @contextmanager
    def unavailable(*args, **kwargs):
        raise GlobalWriterAuthorityTransitionError('global_writer_authority_database_unavailable')
        yield
    monkeypatch.setattr(gate, 'global_writer_authority_lease', unavailable)

    @app.post('/write')
    def writer():
        raise AssertionError('failed lease must precede the handler')

    response = app.test_client().post('/write')
    assert response.status_code == 503
    assert response.get_json()['reason_code'] == 'global_writer_authority_database_unavailable'
    assert response.headers['Cache-Control'] == 'no-store'


def test_read_only_handlers_do_not_acquire_mutation_lease(monkeypatch):
    events = []
    app = application(monkeypatch, events)

    @app.get('/read')
    def read():
        return {'ok': True}

    @app.post('/read-only')
    @cutover_read_only_view
    def read_only():
        return {'ok': True}

    assert app.test_client().get('/read').status_code == 200
    assert app.test_client().post('/read-only').status_code == 200
    assert events == []


def test_all_four_internal_cron_get_and_head_handlers_are_declared_writers():
    from routes import internal_cron
    for name in ('outbox_reconciliation', 'whatsapp_payload_retention',
                 'survey_privacy_retention', 'weekly_analytics_report'):
        assert is_cutover_writer_view(getattr(internal_cron, name))


@pytest.mark.parametrize('authority_enabled', [True, False])
def test_real_native_login_anonymous_adoption_stays_inside_lease_when_enabled(
        monkeypatch, tmp_path, authority_enabled):
    from threading import get_ident
    from tests.profile_acceptance_runtime import create_disposable_app
    from routes import auth
    app, accounts, password = create_disposable_app(tmp_path)
    app.config.update(CUTOVER_GLOBAL_WRITER_AUTHORITY_ENABLED=authority_enabled,
                      CUTOVER_RUNTIME_IDENTITY='vercel', DEFER_ANON_MIGRATION_ON_LOGIN=True)
    events, threads = [], []
    caller_thread = get_ident()
    decision = GlobalWriterAuthorityDecision(True, authority_enabled, 'fixture_admission', 1)
    monkeypatch.setattr(gate, 'evaluate_global_writer_authority', lambda config: decision)

    @contextmanager
    def lease(config, *, request_lifetime):
        events.append('lease_enter')
        try:
            yield SimpleNamespace(decision=decision)
        finally:
            events.append('lease_release')
    monkeypatch.setattr(gate, 'global_writer_authority_lease', lease)

    def adoption(**kwargs):
        assert kwargs['user_id'] == accounts['acceptance-a']['id']
        assert kwargs['anon_id'] == 'bounded_anonymous_fixture'
        assert get_ident() == caller_thread
        assert events == ['lease_enter']
        events.append('adoption_finished')
    monkeypatch.setattr(auth, '_run_post_login_migrations', adoption)

    class DeferredThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
        def start(self):
            threads.append(self.kwargs)
    monkeypatch.setattr(auth.threading, 'Thread', DeferredThread)

    response = app.test_client().post('/auth/login', json={
        'email': accounts['acceptance-a']['email'], 'password': password,
        'anon_id': 'bounded_anonymous_fixture'}, buffered=True)
    assert response.status_code == 200
    assert response.get_json()['session_retirement']['actor_id'] == str(accounts['acceptance-a']['id'])
    if authority_enabled:
        assert threads == []
        assert events == ['lease_enter', 'adoption_finished', 'lease_release']
    else:
        assert len(threads) == 1 and threads[0]['target'] is adoption
        assert events == []
