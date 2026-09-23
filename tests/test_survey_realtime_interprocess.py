from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import socket_service
from socket_service import (
    SurveyRealtimeTransportError,
    build_fail_closed_socketio_redis_manager,
    ensure_survey_realtime_transport_ready,
)


QUEUE_URL = "redis://queue.internal:6379/7"
QUEUE_CHANNEL = "chatboc-realtime-v1"


def _app(*, role: str, queue_url: str = QUEUE_URL) -> Flask:
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        CHATBOC_PROCESS_ROLE=role,
        SOCKETIO_MESSAGE_QUEUE_URL=queue_url,
        SOCKETIO_MESSAGE_QUEUE_CHANNEL=QUEUE_CHANNEL,
        SOCKETIO_MESSAGE_QUEUE_HEALTHCHECK_TIMEOUT_SECONDS=1.25,
    )
    return app


def test_web_without_queue_keeps_local_socketio_compatibility():
    app = _app(role="", queue_url="")

    with app.app_context():
        status = ensure_survey_realtime_transport_ready()

    assert status == {
        "transport": "socketio_in_process",
        "shared": False,
        "verified": True,
    }


def test_serverless_cron_explicitly_requires_shared_queue_in_web_process():
    app = _app(role="web", queue_url="")

    with app.app_context(), pytest.raises(SurveyRealtimeTransportError) as exc:
        ensure_survey_realtime_transport_ready(require_shared=True)

    assert exc.value.code == "survey_realtime_shared_transport_not_configured"


def test_serverless_cron_accepts_verified_subscribed_redis_manager():
    from socketio.redis_manager import RedisManager

    app = _app(role="web")
    manager = RedisManager(
        QUEUE_URL,
        channel=QUEUE_CHANNEL,
        write_only=False,
    )
    redis_client = MagicMock()
    redis_client.ping.return_value = True

    with app.app_context(), patch.object(
        socket_service.socketio,
        "server",
        SimpleNamespace(manager=manager),
    ), patch("redis.Redis.from_url", return_value=redis_client):
        status = ensure_survey_realtime_transport_ready(require_shared=True)

    assert status["transport"] == "socketio_redis_pubsub"
    assert status["shared"] is True
    assert status["verified"] is True
    assert status["publisher_mode"] == "subscriber"


def test_survey_worker_without_shared_queue_fails_closed():
    app = _app(role="survey-effect-worker", queue_url="")

    with app.app_context(), pytest.raises(SurveyRealtimeTransportError) as exc:
        ensure_survey_realtime_transport_ready()

    assert exc.value.code == "survey_realtime_shared_transport_not_configured"


def test_survey_worker_requires_matching_write_only_redis_manager():
    app = _app(role="survey-effect-worker")
    wrong_manager = SimpleNamespace(
        redis_url=QUEUE_URL,
        channel=QUEUE_CHANNEL,
        write_only=True,
    )

    with app.app_context(), patch.object(
        socket_service.socketio,
        "server",
        SimpleNamespace(manager=wrong_manager),
    ), pytest.raises(SurveyRealtimeTransportError) as exc:
        ensure_survey_realtime_transport_ready()

    assert exc.value.code == "survey_realtime_shared_manager_not_initialized"


def test_survey_worker_verifies_redis_immediately_before_publish():
    app = _app(role="survey-effect-worker")
    manager = build_fail_closed_socketio_redis_manager(
        QUEUE_URL,
        channel=QUEUE_CHANNEL,
        timeout_seconds=1.25,
    )
    redis_client = MagicMock()
    redis_client.ping.return_value = True

    with app.app_context(), patch.object(
        socket_service.socketio,
        "server",
        SimpleNamespace(manager=manager),
    ), patch("redis.Redis.from_url", return_value=redis_client) as from_url:
        status = ensure_survey_realtime_transport_ready()

    assert status == {
        "transport": "socketio_redis_pubsub",
        "shared": True,
        "verified": True,
        "channel": QUEUE_CHANNEL,
        "publisher_mode": "write_only",
    }
    from_url.assert_called_once_with(
        QUEUE_URL,
        socket_connect_timeout=1.25,
        socket_timeout=1.25,
        retry_on_timeout=False,
    )
    redis_client.ping.assert_called_once_with()
    redis_client.close.assert_called_once_with()


def test_survey_worker_does_not_accept_unreachable_redis():
    app = _app(role="survey-effect-worker")
    manager = build_fail_closed_socketio_redis_manager(
        QUEUE_URL,
        channel=QUEUE_CHANNEL,
        timeout_seconds=1.25,
    )
    redis_client = MagicMock()
    redis_client.ping.side_effect = ConnectionError("secret queue unavailable")

    with app.app_context(), patch.object(
        socket_service.socketio,
        "server",
        SimpleNamespace(manager=manager),
    ), patch("redis.Redis.from_url", return_value=redis_client), pytest.raises(
        SurveyRealtimeTransportError
    ) as exc:
        ensure_survey_realtime_transport_ready()

    assert exc.value.code == "survey_realtime_shared_transport_unreachable"
    assert "secret" not in str(exc.value)
    redis_client.close.assert_called_once_with()


def test_worker_redis_manager_turns_definite_publish_failure_into_exception():
    manager = build_fail_closed_socketio_redis_manager(
        QUEUE_URL,
        channel=QUEUE_CHANNEL,
        timeout_seconds=1.25,
    )

    assert manager.redis_options == {
        "socket_connect_timeout": 1.25,
        "socket_timeout": 1.25,
        "retry_on_timeout": False,
    }

    with patch(
        "socketio.redis_manager.RedisManager._publish",
        return_value=0,
    ):
        assert manager._publish({"event": "survey.vote.created"}) == 0

    with patch(
        "socketio.redis_manager.RedisManager._publish",
        return_value=None,
    ), pytest.raises(SurveyRealtimeTransportError) as exc:
        manager._publish({"event": "survey.vote.created"})

    assert exc.value.code == "survey_realtime_shared_transport_publish_failed"
