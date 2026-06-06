from services.conversation_stream import (
    CONVERSATION_STREAM_SCHEMA_VERSION,
    build_unified_conversation_stream,
    build_realtime_envelope,
)


def test_build_realtime_envelope_normalizes_ticket_and_message_fields():
    payload = {
        "ticket_id": 44,
        "tenant_type": "municipio",
        "municipio_id": 9,
        "comentario_id": 101,
        "comentario": "Hola mundo",
        "channel": "widget",
        "origin": "public_tracking",
        "author_type": "citizen",
    }

    envelope = build_realtime_envelope(
        event_name="conversation.message.created",
        payload=payload,
        room="municipio_9",
    )

    assert envelope["event_name"] == "conversation.message.created"
    assert envelope["schema_version"] == CONVERSATION_STREAM_SCHEMA_VERSION
    assert envelope["room"] == "municipio_9"
    assert envelope["conversation"]["id"] == "ticket:municipio:44"
    assert envelope["conversation"]["channel"] == "widget"
    assert envelope["conversation"]["origin"] == "public_tracking"
    assert envelope["ticket"]["id"] == 44
    assert envelope["ticket"]["tenant_type"] == "municipio"
    assert envelope["ticket"]["tenant_id"] == 9
    assert envelope["message"]["id"] == 101
    assert envelope["message"]["text"] == "Hola mundo"
    assert envelope["message"]["author_type"] == "citizen"
    assert envelope["payload"] == payload


def test_build_realtime_envelope_preserves_explicit_conversation_id():
    payload = {
        "conversation_id": "conv-123",
        "id": 88,
        "tenant_type": "pyme",
        "pyme_id": 5,
        "estado": "nuevo",
    }

    envelope = build_realtime_envelope(
        event_name="ticket.status.changed",
        payload=payload,
    )

    assert envelope["conversation"]["id"] == "conv-123"
    assert envelope["ticket"]["id"] == 88
    assert envelope["ticket"]["status"] == "nuevo"


def test_build_unified_conversation_stream_normalizes_render_contract():
    stream = build_unified_conversation_stream(
        timeline=[
            {"tipo": "ticket_creado", "fecha": "2026-03-21T10:00:00+00:00"},
            {"tipo": "estado", "estado": "en_proceso", "fecha": "2026-03-21T10:01:00+00:00"},
        ],
        historial_chat=[
            {
                "id": 44,
                "texto": "Necesito ayuda",
                "fecha": "2026-03-21T10:02:00+00:00",
                "autor": "vecino",
                "autor_nombre": "Ana",
                "es_admin": False,
            }
        ],
        latest_comment_id=44,
    )

    assert [item["source"] for item in stream] == ["timeline", "timeline", "chat_history"]
    assert stream[0]["actor_type"] == "system"
    assert stream[0]["preview_text"] == "Ticket creado"
    assert stream[1]["status"] == "en_proceso"
    assert stream[1]["badge"] == "status_change"
    assert stream[2]["id"] == "chat_history:44"
    assert stream[2]["actor_type"] == "citizen"
    assert stream[2]["is_unread"] is False


def test_build_unified_conversation_stream_dedupes_timeline_and_chat_messages():
    stream = build_unified_conversation_stream(
        timeline=[
            {"tipo": "ticket_creado", "fecha": "2026-03-21T10:00:00+00:00"},
            {
                "tipo": "comentario",
                "id": 44,
                "texto": "Necesito ayuda",
                "fecha": "2026-03-21T10:02:00+00:00",
                "autor": "vecino",
                "autor_nombre": "Ana",
                "es_admin": False,
            },
        ],
        historial_chat=[
            {
                "id": 44,
                "texto": "Necesito ayuda",
                "fecha": "2026-03-21T10:02:00+00:00",
                "autor": "vecino",
                "autor_nombre": "Ana",
                "es_admin": False,
            }
        ],
        latest_comment_id=44,
    )

    message_items = [item for item in stream if item["stream_type"] in {"message", "comentario"}]
    assert len(message_items) == 1
    assert message_items[0]["source"] == "chat_history"
    assert message_items[0]["id"] == "chat_history:44"
