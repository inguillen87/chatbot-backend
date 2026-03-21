import jwt

from datetime import timedelta

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TicketComentario, TicketRealtimeState, User
from routes.ticket import serialize_ticket_to_json
from services.ticket_realtime_state import build_ticket_realtime_summary
from utils.time_utils import get_local_now


def _headers(app, user):
    token = jwt.encode(
        {"user_id": user.id, "rol": user.rol, "tipo_chat": user.tipo_chat},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Chat-Session-Id": "session-rt-1"}


def test_ticket_presence_and_read_state_endpoints():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        client = app.test_client()

        owner = User(email="owner-rt@test.com", name="Owner RT", rol="admin", tipo_chat="municipio", municipio_id=77)
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=77,
            user_id=owner.id,
            pregunta="Necesito ayuda",
            asunto="Realtime",
            estado="nuevo",
            nombre_vecino="Vecino RT",
        )
        db.session.add(ticket)
        db.session.commit()

        comment = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario="Primer mensaje",
            user_id=owner.id,
            es_admin=True,
        )
        db.session.add(comment)
        db.session.commit()

        presence_resp = client.post(
            f"/tickets/municipio/{ticket.id}/presence",
            json={"presence_status": "active"},
            headers=_headers(app, owner),
        )
        assert presence_resp.status_code == 200
        presence_body = presence_resp.get_json()
        assert presence_body["presence"]["presence_status"] == "active"
        assert presence_body["realtime_state"]["presence"]["active_count"] == 1

        read_resp = client.post(
            f"/tickets/municipio/{ticket.id}/read-state",
            json={"last_read_comment_id": comment.id},
            headers=_headers(app, owner),
        )
        assert read_resp.status_code == 200
        read_body = read_resp.get_json()
        assert read_body["read_state"]["last_read_comment_id"] == comment.id
        assert read_body["realtime_state"]["read_state"]["viewers"][0]["viewer_key"] == f"user:{owner.id}"
        assert read_body["realtime_state"]["read_state"]["latest_comment_id"] == comment.id
        assert read_body["realtime_state"]["read_state"]["unread_viewer_count"] == 0

        chat_resp = client.get(
            f"/tickets/chat/{ticket.id}/mensajes",
            headers=_headers(app, owner),
        )
        assert chat_resp.status_code == 200
        chat_body = chat_resp.get_json()
        assert chat_body["realtime_state"]["presence"]["active_count"] == 1
        assert chat_body["realtime_state"]["read_state"]["viewers"][0]["last_read_comment_id"] == comment.id

        second_comment = TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario="Segundo mensaje",
            user_id=owner.id,
            es_admin=True,
        )
        db.session.add(second_comment)
        db.session.commit()

        ticket_payload = serialize_ticket_to_json(ticket, "municipio")
        assert ticket_payload["collaboration_state"]["active_viewers_count"] == 1
        assert ticket_payload["collaboration_state"]["idle_viewers_count"] == 0
        assert ticket_payload["collaboration_state"]["latest_comment_id"] == second_comment.id
        assert ticket_payload["collaboration_state"]["unread_viewer_count"] == 1

        realtime_state = TicketRealtimeState.query.first()
        realtime_state.last_presence_at = get_local_now() - timedelta(minutes=10)
        db.session.commit()
        summary_idle = build_ticket_realtime_summary(ticket_type="municipio", ticket_id=ticket.id)
        assert summary_idle["presence"]["active_count"] == 0
        assert summary_idle["presence"]["idle_count"] == 1
        assert summary_idle["read_state"]["viewers"][0]["effective_presence_status"] == "idle"

        assert TicketRealtimeState.query.count() == 1
