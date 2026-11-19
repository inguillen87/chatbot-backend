import sys
from types import SimpleNamespace

import pytest
from flask import Flask

import routes.health as health_routes


@pytest.fixture
def probe_client(monkeypatch):
    calls = {"collections": 0}
    holder = {"client": None, "error": None}

    class Client:
        def get_collections(self):
            calls["collections"] += 1
            if holder["error"] is not None:
                raise holder["error"]
            return SimpleNamespace(collections=[])

    def get_qdrant_client():
        return holder["client"]

    monkeypatch.setitem(
        sys.modules,
        "services.qdrant_utils",
        SimpleNamespace(get_qdrant_client=get_qdrant_client),
    )
    monkeypatch.setattr(health_routes, "_vector_index_probe", {"at": None, "state": None})
    clock = {"now": 1000.0}
    monkeypatch.setattr(health_routes.time, "monotonic", lambda: clock["now"])
    app = Flask(__name__)
    app.register_blueprint(health_routes.health_bp)
    return SimpleNamespace(
        http=app.test_client(), calls=calls, holder=holder, clock=clock, Client=Client
    )


def test_connected_index_answers_ok_and_is_probed_once_per_window(probe_client):
    probe_client.holder["client"] = probe_client.Client()

    first = probe_client.http.get("/api/health/vector-index")
    second = probe_client.http.get("/api/health/vector-index")

    assert first.status_code == 200
    assert first.get_json() == {
        "contract_version": "chatboc.vector_index_health.v1",
        "status": "connected",
        "cached": False,
    }
    assert first.headers["Cache-Control"] == "no-store"
    assert second.get_json()["cached"] is True
    # A burst of requests does not become a burst of provider calls.
    assert probe_client.calls["collections"] == 1

    probe_client.clock["now"] += health_routes._VECTOR_INDEX_PROBE_TTL_SECONDS + 1
    third = probe_client.http.get("/api/health/vector-index")
    assert third.get_json()["cached"] is False
    assert probe_client.calls["collections"] == 2


def test_unreachable_index_is_reported_without_provider_detail(probe_client, caplog):
    probe_client.holder["client"] = probe_client.Client()
    probe_client.holder["error"] = ConnectionResetError(
        "https://secret-cluster.example:6333 api-key=do-not-leak"
    )

    response = probe_client.http.get("/api/health/vector-index")
    serialized = response.get_data(as_text=True)

    assert response.status_code == 503
    assert response.get_json()["status"] == "error"
    assert "secret-cluster" not in serialized
    assert "do-not-leak" not in serialized
    assert "secret-cluster" not in caplog.text
    assert "do-not-leak" not in caplog.text
    assert "ConnectionResetError" in caplog.text


def test_a_failure_is_held_briefly_and_recovery_shows_within_half_a_minute(probe_client):
    probe_client.holder["client"] = probe_client.Client()
    probe_client.holder["error"] = TimeoutError("down")
    assert probe_client.http.get("/api/health/vector-index").status_code == 503
    # Hammering a failing index does not reach the provider each time.
    repeated = probe_client.http.get("/api/health/vector-index")
    assert repeated.status_code == 503
    assert repeated.get_json()["cached"] is True
    assert probe_client.calls["collections"] == 1

    probe_client.holder["error"] = None
    probe_client.clock["now"] += health_routes._VECTOR_INDEX_FAILURE_TTL_SECONDS + 1
    recovered = probe_client.http.get("/api/health/vector-index")

    assert recovered.status_code == 200
    assert recovered.get_json() == {
        "contract_version": "chatboc.vector_index_health.v1",
        "status": "connected",
        "cached": False,
    }


def test_missing_configuration_is_distinguished_from_a_failure(probe_client):
    response = probe_client.http.get("/api/health/vector-index")

    assert response.status_code == 503
    assert response.get_json()["status"] == "not_configured"
    assert probe_client.calls["collections"] == 0
