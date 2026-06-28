import pytest
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import patch

from app import db
from models import ChatSessionContext, User, Rubro
from services.live_chat_schedule import (
    build_live_chat_status,
    build_tenant_live_chat_status,
    detect_urgency_reason,
    get_schedule_description,
    is_live_chat_available,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.pymes import CONTEXTO_PYME


def test_detect_urgency_reason_keywords():
    assert detect_urgency_reason("Esto es URGENTE, hubo un siniestro") is not None
    assert detect_urgency_reason("Necesito ayuda urgente por favor") is not None
    assert detect_urgency_reason("solo consulto por trámites") is None


def test_is_live_chat_available_default_schedule(app):
    with app.app_context():
        monday = datetime(2024, 7, 1, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        outside = datetime(2024, 7, 1, 14, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        weekend = datetime(2024, 7, 6, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        assert is_live_chat_available(monday)
        assert not is_live_chat_available(outside)
        assert not is_live_chat_available(weekend)


@pytest.fixture
def municipio_context(app, client, owner_user, viewer_user):
    with app.app_context():
        chat_ctx = ChatSessionContext(chat_session_id="test_muni", user_id=owner_user.id, context_data={})
        db.session.add(chat_ctx)
        db.session.commit()
        yield chat_ctx
        db.session.delete(chat_ctx)
        db.session.commit()


def test_municipio_auto_live_chat_triggers(app, owner_user, viewer_user, municipio_context):
    from services.municipio_responder import responder_municipio

    with app.app_context(), patch(
        "services.municipio_responder.is_live_chat_available", return_value=True
    ), patch(
        "services.actions.municipio_actions.DerivarHumanoActionHandler.execute",
        return_value={
            "success": True,
            "message_to_user": "Conectando con un agente",
            "data": {"ticket_id": 1, "chat_id": "M-123", "status": "esperando_agente_en_vivo"},
        },
    ) as mock_execute:
        response = responder_municipio(
            "Es urgente, hubo un siniestro en la plaza",
            owner_user,
            owner_user.rubro,
            viewer_user=viewer_user,
            chat_db_context=municipio_context,
            anon_id="anon-1",
            channel="web",
        )
        assert response["fuente"] == "auto_live_chat_urgente"
        assert response["data"]["ticket_id"] == 1
        assert response["contexto_actualizado"][CONTEXTO_MUNICIPIO]["live_chat_ticket_id"] == 1
        mock_execute.assert_called_once()
        assert municipio_context.context_data["contexto_municipio_v2"]["live_chat_autoderivado"] is True
        assert municipio_context.context_data["contexto_municipio_v2"]["live_chat_ticket_id"] == 1
        assert municipio_context.context_data["contexto_municipio_v2"]["live_chat_estado"] == "esperando_agente_en_vivo"


@pytest.fixture
def pyme_owner(app, client, init_database):
    with app.app_context():
        rubro = Rubro.query.first()
        pyme = User(
            name="Pyme Owner",
            email="pyme@test.com",
            rol="admin",
            tipo_chat="pyme",
            token="pyme-token",
            rubro_id=rubro.id if rubro else None,
        )
        pyme.set_password("test")
        db.session.add(pyme)
        db.session.commit()
        yield pyme
        db.session.delete(pyme)
        db.session.commit()


@pytest.fixture
def pyme_context(app, pyme_owner):
    with app.app_context():
        chat_ctx = ChatSessionContext(chat_session_id="test_pyme", user_id=pyme_owner.id, context_data={})
        db.session.add(chat_ctx)
        db.session.commit()
        yield chat_ctx
        db.session.delete(chat_ctx)
        db.session.commit()


def test_pyme_auto_live_chat_triggers(app, pyme_owner, viewer_user, pyme_context):
    from services.pymes import responder_pyme

    with app.app_context(), patch(
        "services.pymes.is_live_chat_available", return_value=True
    ), patch(
        "services.actions.pyme_actions.DerivarHumanoActionHandlerPyme.execute",
        return_value={
            "success": True,
            "message_to_user": "Conectando con un asesor",
            "data": {"ticket_id": 2, "chat_id": "P-555", "status": "esperando_agente_en_vivo"},
        },
    ) as mock_execute:
        response = responder_pyme(
            "Necesito ayuda urgente, hubo un accidente",
            pyme_owner,
            pyme_owner.rubro,
            viewer_user=viewer_user,
            chat_db_context=pyme_context,
            anon_id="anon-2",
            channel="web",
        )
        assert response["fuente"] == "auto_live_chat_urgente"
        assert response["data"]["ticket_id"] == 2
        assert response["contexto_actualizado"][CONTEXTO_PYME]["live_chat_ticket_id"] == 2
        mock_execute.assert_called_once()
        assert pyme_context.context_data["contexto_pyme_v2"]["live_chat_autoderivado"] is True
        assert pyme_context.context_data["contexto_pyme_v2"]["live_chat_ticket_id"] == 2
        assert pyme_context.context_data["contexto_pyme_v2"]["live_chat_estado"] == "esperando_agente_en_vivo"


def test_schedule_description_contains_range(app):
    with app.app_context():
        description = get_schedule_description()
        assert "lunes" in description.lower() or "viernes" in description.lower()
        assert "hs" in description


def test_live_chat_status_description_uses_schedule_override(app):
    with app.app_context():
        status = build_live_chat_status(
            schedule_override={
                "enabled": True,
                "days": ["lunes", "martes"],
                "start_time": "15:30",
                "end_time": "19:45",
                "timezone": "America/Argentina/Buenos_Aires",
            }
        )
        assert status["start_time"] == "15:30"
        assert status["end_time"] == "19:45"
        assert "15:30" in status["description"]
        assert "19:45" in status["description"]


def test_tenant_live_chat_status_exposes_online_and_offline_cta(app):
    tenant = SimpleNamespace(
        id=7,
        slug="muni-demo",
        configuracion={
            "live_chat_schedule": {
                "enabled": True,
                "days": ["lunes"],
                "start_time": "09:00",
                "end_time": "13:00",
                "timezone": "America/Argentina/Buenos_Aires",
            }
        },
    )

    with app.app_context():
        online = datetime(2024, 7, 1, 10, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        online_status = build_tenant_live_chat_status(tenant, now=online, socket_room="municipio_7")
        assert online_status["mode"] == "live"
        assert online_status["availability_state"] == "online"
        assert online_status["cta"]["primary"]["action"] == "open_live_chat"
        assert online_status["ui"]["primary_cta_label"] == "Chatear con un agente"
        assert online_status["socket_room"] == "municipio_7"

        offline = datetime(2024, 7, 1, 18, 0, tzinfo=ZoneInfo("America/Argentina/Buenos_Aires"))
        offline_status = build_tenant_live_chat_status(tenant, now=offline, socket_room="municipio_7")
        assert offline_status["mode"] == "offline"
        assert offline_status["availability_state"] == "offline_accepting_messages"
        assert offline_status["cta"]["primary"]["action"] == "queue_offline_message"
        assert offline_status["ui"]["primary_cta_label"] == "Dejar mensaje"
        assert offline_status["offline_message"]["safe_when_outside_hours"] is True
        assert offline_status["channel_policy"]["frontend_must_keep_user_in_ticket_context"] is True
