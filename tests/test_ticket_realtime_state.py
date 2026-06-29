import jwt

from datetime import timedelta

from app import create_app, db
from config import TestConfig
from models import MunicipioTicket, TicketComentario, TicketRealtimeState, User
from routes.ticket import serialize_ticket_to_json
from services.ticket_realtime_state import (
    build_ticket_collaboration_states,
    build_ticket_realtime_summary,
    prune_stale_ticket_realtime_states,
)
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
        assert ticket_payload["collaboration_state"]["operational_status"] == "actively_managed"
        assert "collaboration_hint" in ticket_payload["collaboration_state"]

        realtime_state = TicketRealtimeState.query.first()
        realtime_state.last_presence_at = get_local_now() - timedelta(minutes=10)
        db.session.commit()
        summary_idle = build_ticket_realtime_summary(ticket_type="municipio", ticket_id=ticket.id)
        assert summary_idle["presence"]["active_count"] == 0
        assert summary_idle["presence"]["idle_count"] == 1
        assert summary_idle["read_state"]["viewers"][0]["effective_presence_status"] == "idle"
        assert summary_idle["meta"]["viewer_rows_considered"] == 1

        assert TicketRealtimeState.query.count() == 1


def test_realtime_summary_dedupes_viewers_and_prunes_stale_rows():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(email="owner-rt-dedupe@test.com", name="Owner RT", rol="admin", tipo_chat="municipio", municipio_id=90)
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=90,
            user_id=owner.id,
            pregunta="Necesito ayuda",
            asunto="Realtime stale",
            estado="nuevo",
            nombre_vecino="Vecino RT",
        )
        db.session.add(ticket)
        db.session.commit()

        now = get_local_now()
        db.session.add(
            TicketRealtimeState(
                ticket_type="municipio",
                ticket_id=ticket.id,
                viewer_key=f"user:{owner.id}:old",
                viewer_user_id=owner.id,
                viewer_role="admin",
                active_session_id="old-session",
                presence_status="idle",
                last_presence_at=now - timedelta(minutes=12),
                last_read_comment_id=0,
            )
        )
        db.session.add(
            TicketRealtimeState(
                ticket_type="municipio",
                ticket_id=ticket.id,
                viewer_key=f"user:{owner.id}:new",
                viewer_user_id=owner.id,
                viewer_role="admin",
                active_session_id="new-session",
                presence_status="active",
                last_presence_at=now,
                last_read_comment_id=0,
            )
        )
        db.session.add(
            TicketRealtimeState(
                ticket_type="municipio",
                ticket_id=ticket.id,
                viewer_key="anon:stale",
                viewer_anon_id="stale",
                viewer_role="anonymous",
                presence_status="inactive",
                last_presence_at=now - timedelta(hours=48),
                last_read_comment_id=0,
            )
        )
        db.session.commit()

        deleted = prune_stale_ticket_realtime_states(now=now)
        assert deleted == 1
        summary = build_ticket_realtime_summary(ticket_type="municipio", ticket_id=ticket.id)
        assert summary["presence"]["active_count"] == 1
        assert summary["meta"]["viewer_rows_considered"] == 1


def test_realtime_summary_unread_count_counts_comments_not_id_gap():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(email="owner-rt-count@test.com", name="Owner RT", rol="admin", tipo_chat="municipio", municipio_id=91)
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=91,
            user_id=owner.id,
            pregunta="Necesito ayuda",
            asunto="Realtime unread",
            estado="nuevo",
            nombre_vecino="Vecino RT",
        )
        other_ticket = MunicipioTicket(
            municipio_id=91,
            user_id=owner.id,
            pregunta="Otro ticket",
            asunto="Otro",
            estado="nuevo",
            nombre_vecino="Vecino RT",
        )
        db.session.add_all([ticket, other_ticket])
        db.session.commit()

        db.session.add(TicketComentario(municipio_ticket_id=other_ticket.id, comentario="Otro 1", user_id=owner.id, es_admin=True))
        db.session.add(TicketComentario(municipio_ticket_id=other_ticket.id, comentario="Otro 2", user_id=owner.id, es_admin=True))
        first = TicketComentario(municipio_ticket_id=ticket.id, comentario="Primer mensaje", user_id=owner.id, es_admin=True)
        second = TicketComentario(municipio_ticket_id=ticket.id, comentario="Segundo mensaje", user_id=owner.id, es_admin=True)
        db.session.add_all([first, second])
        db.session.commit()

        db.session.add(
            TicketRealtimeState(
                ticket_type="municipio",
                ticket_id=ticket.id,
                viewer_key=f"user:{owner.id}",
                viewer_user_id=owner.id,
                viewer_role="admin",
                presence_status="active",
                last_presence_at=get_local_now(),
                last_read_comment_id=first.id,
            )
        )
        db.session.commit()

        summary = build_ticket_realtime_summary(ticket_type="municipio", ticket_id=ticket.id)
        viewer = summary["read_state"]["viewers"][0]
        assert viewer["latest_comment_id"] == second.id
        assert viewer["last_read_comment_id"] == first.id
        assert viewer["unread_count"] == 1


def test_compact_inbox_collaboration_state_uses_bulk_realtime_summary():
    app = create_app(TestConfig)
    with app.app_context():
        db.create_all()
        owner = User(email="owner-rt-bulk@test.com", name="Owner RT", rol="admin", tipo_chat="municipio", municipio_id=92)
        owner.set_password("pass")
        db.session.add(owner)
        db.session.commit()

        ticket = MunicipioTicket(
            municipio_id=92,
            user_id=owner.id,
            pregunta="Necesito ayuda",
            asunto="Realtime bulk",
            estado="nuevo",
            nombre_vecino="Vecino RT",
        )
        db.session.add(ticket)
        db.session.commit()

        first = TicketComentario(municipio_ticket_id=ticket.id, comentario="Primer mensaje", user_id=owner.id, es_admin=True)
        second = TicketComentario(municipio_ticket_id=ticket.id, comentario="Segundo mensaje", user_id=owner.id, es_admin=True)
        db.session.add_all([first, second])
        db.session.commit()

        db.session.add(
            TicketRealtimeState(
                ticket_type="municipio",
                ticket_id=ticket.id,
                viewer_key=f"user:{owner.id}",
                viewer_user_id=owner.id,
                viewer_role="admin",
                presence_status="active",
                last_presence_at=get_local_now(),
                last_read_comment_id=first.id,
            )
        )
        db.session.commit()

        states = build_ticket_collaboration_states(
            ticket_type="municipio",
            ticket_ids=[ticket.id],
            latest_comment_ids={ticket.id: second.id},
            comment_counts={ticket.id: 2},
        )

        assert states[ticket.id]["active_viewers_count"] == 1
        assert states[ticket.id]["latest_comment_id"] == second.id
        assert states[ticket.id]["unread_viewer_count"] == 1

        payload = serialize_ticket_to_json(
            ticket,
            "municipio",
            compact=True,
            comentarios_count_override=2,
            collaboration_state_override=states[ticket.id],
        )

        assert payload["comentarios"] == []
        assert payload["historial_chat"] == []
        assert payload["comentarios_count"] == 2
        assert payload["collaboration_state"]["latest_comment_id"] == second.id
        assert payload["collaboration_state"]["active_viewers_count"] == 1
