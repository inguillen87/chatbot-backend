from datetime import datetime, timedelta

from app import db
from models import Conversacion
from routes.chat import _anonymous_message_count


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
