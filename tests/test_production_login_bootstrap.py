"""Original login joins readiness once; other writes and timeout stay closed."""
from io import BytesIO
import json
import os
import threading
import time
from unittest.mock import patch

import pytest

from bootstrap_wsgi import LazyApplication


def production_app(loader, seconds=.3):
    with patch.dict(os.environ, {"VERCEL_ENV": "production"}, clear=True):
        return LazyApplication(loader, background_warmup=True,
            warmup_delay_seconds=0, safe_request_wait_seconds=seconds)


@pytest.mark.parametrize("path", ["/api/auth/clerk/session", "/auth/clerk/session",
    "/api/auth/admin/login", "/auth/admin/login"])
def test_original_production_login_dispatches_once_after_readiness(path):
    started, release = threading.Event(), threading.Event()
    observed = []
    def target(environ, start_response):
        observed.append((environ, environ["wsgi.input"].read(), environ["HTTP_X_REQUEST_ID"]))
        start_response("200 OK", [("Set-Cookie", "fixture-session=1")])
        return [b"canonical-result"]
    def loader():
        started.set()
        assert release.wait(1)
        return target
    app = production_app(loader)
    body = BytesIO(b"opaque-existing-login")
    environ = {"REQUEST_METHOD": "POST", "PATH_INFO": path, "wsgi.input": body,
        "CONTENT_LENGTH": "21", "HTTP_X_REQUEST_ID": "original-login-1"}
    statuses, headers, result = [], [], []
    request = threading.Thread(target=lambda: result.extend(app(environ,
        lambda status, values: (statuses.append(status), headers.extend(values)))))
    request.start()
    assert started.wait(.5)
    assert observed == [] and body.tell() == 0
    release.set()
    request.join(1)
    assert not request.is_alive()
    assert observed == [(environ, b"opaque-existing-login", "original-login-1")]
    assert statuses == ["200 OK"] and result == [b"canonical-result"]
    assert dict(headers)["Set-Cookie"] == "fixture-session=1"


def test_timed_out_login_is_never_dispatched_later_or_replayed():
    release = threading.Event()
    dispatches = []
    def target(environ, start_response):
        dispatches.append(environ)
        start_response("200 OK", [])
        return [b"ok"]
    def loader():
        assert release.wait(1)
        return target
    app = production_app(loader, .02)
    body = BytesIO(b"original-login-body")
    statuses, headers = [], []
    response = app({"REQUEST_METHOD": "POST", "PATH_INFO": "/api/auth/clerk/session",
        "wsgi.input": body}, lambda status, values: (statuses.append(status), headers.extend(values)))
    payload = json.loads(b"".join(response))
    assert statuses == ["503 Service Unavailable"]
    assert payload["request_dispatched"] is False and payload["retryable"] is True
    assert body.tell() == 0 and dispatches == []
    assert dict(headers)["Cache-Control"] == "no-store" and "Set-Cookie" not in dict(headers)
    release.set()
    assert app._ready.wait(.5)
    assert body.tell() == 0 and dispatches == []


@pytest.mark.parametrize("method,path", [("PUT", "/api/auth/clerk/session"),
    ("POST", "/api/auth/clerk/session/extra"), ("POST", "/api/auth/clerk/onboarding"),
    ("POST", "/api/auth/password/reset/request"), ("POST", "/api/orders"),
    ("POST", "/api/auth/clerk/webhook"), ("DELETE", "/api/tickets/1")])
def test_other_production_writes_keep_fail_fast_and_leave_body_untouched(method, path):
    release = threading.Event()
    def loader():
        assert release.wait(1)
        return lambda environ, start_response: [b"unexpected"]
    app = production_app(loader)
    body = BytesIO(b"original-body")
    with patch.object(app._ready, "wait", side_effect=AssertionError("Other writes must not join")):
        response = app({"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": body},
            lambda status, values: None)
    assert json.loads(b"".join(response))["request_dispatched"] is False and body.tell() == 0
    release.set()
    assert app._ready.wait(.5)


def test_initialization_failure_keeps_login_closed_without_exposing_details():
    def loader():
        raise RuntimeError("private-fixture-detail")
    app = production_app(loader)
    body = BytesIO(b"opaque-login")
    response = b"".join(app({"REQUEST_METHOD": "POST", "PATH_INFO": "/api/auth/admin/login",
        "wsgi.input": body}, lambda status, values: None))
    payload = json.loads(response)
    assert payload["reason_code"] == "application_initialization_failed"
    assert payload["request_dispatched"] is False and payload["retryable"] is False
    assert b"private-fixture-detail" not in response and body.tell() == 0


def test_concurrent_logins_share_loader_and_each_original_dispatches_once():
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    loads, dispatches, statuses = [], [], []
    def target(environ, start_response):
        with lock:
            dispatches.append((environ["HTTP_X_REQUEST_ID"], environ["wsgi.input"].read()))
        start_response("200 OK", [])
        return [b"ok"]
    def loader():
        loads.append(1)
        started.set()
        assert release.wait(1)
        return target
    app = production_app(loader)
    def invoke(index):
        def capture(status, values):
            with lock:
                statuses.append(status)
        app({"REQUEST_METHOD": "POST", "PATH_INFO": "/api/auth/clerk/session",
            "HTTP_X_REQUEST_ID": str(index), "wsgi.input": BytesIO(str(index).encode())}, capture)
    threads = [threading.Thread(target=invoke, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    assert started.wait(.5)
    assert dispatches == []
    release.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert loads == [1] and statuses == ["200 OK"] * 12
    assert sorted(dispatches) == sorted((str(index), str(index).encode()) for index in range(12))


@pytest.mark.parametrize("environment", ["preview", "development", ""])
def test_other_environments_keep_their_existing_auth_policy(environment):
    with patch.dict(os.environ, {"VERCEL_ENV": environment}, clear=True):
        app = LazyApplication(lambda: None, background_warmup=True, safe_request_wait_seconds=4)
    assert app._production_login_wait_seconds == 0


def test_login_wait_uses_existing_finite_read_budget_and_loaded_app_does_not_wait():
    with patch.dict(os.environ, {"VERCEL_ENV": "production"}, clear=True):
        app = LazyApplication(lambda: (lambda environ, start_response: [b"ready"]),
            background_warmup=True, safe_request_wait_seconds=999)
    assert app._production_login_wait_seconds == 5
    app._load_and_cache()
    with patch.object(app._ready, "wait", side_effect=AssertionError("Warm app must not wait")):
        assert app({"REQUEST_METHOD": "POST", "PATH_INFO": "/api/auth/clerk/session"},
            lambda status, values: None) == [b"ready"]


@pytest.mark.parametrize("method,path", [("GET", "/api/auth/clerk/config"),
    ("POST", "/api/auth/clerk/session")])
def test_explicit_five_second_budget_covers_measured_4550ms_startup(method, path):
    dispatches = []
    def target(environ, start_response):
        dispatches.append(environ)
        start_response("200 OK", [])
        return [b"canonical-ready"]
    def loader():
        time.sleep(4.55)
        return target
    app = production_app(loader, 5)
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "wsgi.input": BytesIO(b"original")}
    statuses = []
    result = app(environ, lambda status, values: statuses.append(status))
    assert result == [b"canonical-ready"] and statuses == ["200 OK"]
    assert dispatches == [environ]
