from datetime import datetime, timedelta

from app import db
from models import Conversacion
from routes.chat import _anonymous_message_count, _should_enforce_owner_plan_limit


def test_anonymous_message_count_excludes_init_payload(client):
    session_id = "anon-limit-session"
    now = datetime.utcnow()

    db.session.add(
        Conversacion(
            pregunta="__INIT__",
            respuesta="hola",
            fuente="test",
            session_id=session_id,
            timestamp=now,
        )
    )
    db.session.add(
        Conversacion(
            pregunta="Necesito ayuda",
            respuesta="ok",
            fuente="test",
            session_id=session_id,
            timestamp=now,
        )
    )
    db.session.add(
        Conversacion(
            pregunta="Mensaje viejo",
            respuesta="ok",
            fuente="test",
            session_id=session_id,
            timestamp=now - timedelta(minutes=90),
        )
    )
    db.session.commit()

    count = _anonymous_message_count(session_id, window_minutes=30)
    assert count == 1


def test_should_enforce_owner_plan_limit_skips_public_widget_entity_token_flow():
    enforce = _should_enforce_owner_plan_limit(
        demo_flow_active=False,
        is_init_request=False,
        is_public_landing=True,
        is_anonymous=True,
        has_entity_token=True,
    )
    assert enforce is False


def test_should_enforce_owner_plan_limit_keeps_regular_flow_guarded():
    enforce = _should_enforce_owner_plan_limit(
        demo_flow_active=False,
        is_init_request=False,
        is_public_landing=False,
        is_anonymous=True,
        has_entity_token=False,
    )
    assert enforce is True
