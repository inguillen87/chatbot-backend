from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt

from extensions import db
from models import (
    AuditEvent,
    Conversation,
    Message,
    TenantProfile,
    TenantTicket,
    TenantTicketReplyEvent,
    User,
)


def _tenant_with_users(slug: str) -> tuple[TenantProfile, User, User]:
    admin = User(
        name=f"Admin {slug}",
        email=f"admin-{slug}@example.test",
        rol="admin",
        tenant_slug=slug,
        tipo_chat="municipio",
    )
    admin.set_password("test-password")
    citizen = User(
        name=f"Citizen {slug}",
        email=f"citizen-{slug}@example.test",
        rol="usuario",
        tenant_slug=slug,
        tipo_chat="municipio",
    )
    citizen.set_password("test-password")
    db.session.add_all([admin, citizen])
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        pyme_id=admin.id,
    )
    db.session.add(tenant)
    db.session.flush()
    admin.tenant_id = tenant.id
    citizen.tenant_id = tenant.id
    db.session.add_all([admin, citizen])
    return tenant, admin, citizen


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tenant_slug": tenant_slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant_slug}


def _ticket_with_conversation(
    tenant: TenantProfile,
    citizen: User,
    *,
    conversation_id: str,
    comments: list[dict] | None = None,
) -> tuple[TenantTicket, Conversation]:
    conversation = Conversation(
        id=conversation_id,
        tenant_id=tenant.id,
        created_by_user_id=citizen.id,
        status="active",
    )
    db.session.add(conversation)
    db.session.flush()
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=citizen.id,
        categoria="general",
        descripcion="Historial SQL del reclamo",
        estado="nuevo",
        origen="whatsapp",
        datos_extra={
            "title": "Historial SQL",
            "conversation_id": conversation.id,
            "comments": comments or [],
        },
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket, conversation


def test_v2_timeline_pages_125_same_timestamp_sql_messages_without_overlap(client, app):
    tenant, admin, citizen = _tenant_with_users("endpoint-history-large")
    ticket, conversation = _ticket_with_conversation(
        tenant,
        citizen,
        conversation_id="endpoint-history-large-conv",
    )
    tied_at = datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
    db.session.bulk_save_objects(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user" if index % 2 else "assistant",
                direction="in" if index % 2 else "out",
                body=f"sql-message-{index:03d}",
                meta_payload={"visibility": "public"},
                created_at=tied_at,
            )
            for index in range(1, 126)
        ]
    )
    db.session.commit()
    headers = _auth_headers(app, admin, tenant.slug)

    cursor = None
    pages: list[list[dict]] = []
    for _ in range(4):
        query = {"limit": 50}
        if cursor:
            query["cursor"] = cursor
        response = client.get(
            f"/api/v2/tickets/{ticket.id}/timeline",
            query_string=query,
            headers=headers,
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json() or {}
        unified = payload.get("unified_conversation_stream") or []
        assert len(payload.get("historial_chat") or []) == len(unified)
        assert (payload.get("realtime_state") or {}).get("meta", {}).get("source") == "tickets.v2.normalized_history"
        pages.append(unified)
        if not payload.get("has_more"):
            assert payload.get("next_cursor") is None
            break
        cursor = payload.get("next_cursor")
        assert cursor
    else:  # pragma: no cover - protects against a cursor that never advances
        raise AssertionError("endpoint pagination did not terminate")

    assert [len(page) for page in pages] == [50, 50, 25]
    all_ids = [item["id"] for page in pages for item in page]
    assert len(all_ids) == 125
    assert len(set(all_ids)) == 125
    reconstructed = []
    for page in pages:
        reconstructed = page + reconstructed
    assert [item["payload"]["body"] for item in reconstructed] == [
        f"sql-message-{index:03d}" for index in range(1, 126)
    ]

    invalid_limit = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        query_string={"limit": 101},
        headers=headers,
    )
    assert invalid_limit.status_code == 400
    assert (invalid_limit.get_json() or {}).get("reason_code") == "invalid_history_pagination"


def test_v2_timeline_projects_public_and_internal_normalized_sources(client, app):
    tenant, admin, citizen = _tenant_with_users("endpoint-history-visibility")
    now = datetime(2026, 8, 11, 12, tzinfo=timezone.utc)
    ticket, conversation = _ticket_with_conversation(
        tenant,
        citizen,
        conversation_id="endpoint-history-visibility-conv",
        comments=[
            {"id": "fallback-public", "body": "fallback publico", "visibility": "public", "created_at": now.isoformat()},
            {"id": "fallback-internal", "body": "fallback interno", "visibility": "internal", "created_at": now.isoformat()},
        ],
    )
    db.session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user",
                direction="in",
                body="mensaje publico",
                meta_payload={"visibility": "public"},
                created_at=now + timedelta(seconds=1),
            ),
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="agent",
                direction="out",
                body="mensaje interno",
                meta_payload={"visibility": "internal", "private": "no exponer"},
                created_at=now + timedelta(seconds=2),
            ),
            TenantTicketReplyEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                event_id="endpoint-reply-public",
                body="reply publico",
                visibility="public",
                created_at=now + timedelta(seconds=3),
            ),
            TenantTicketReplyEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                event_id="endpoint-reply-internal",
                body="reply interno",
                visibility="internal",
                created_at=now + timedelta(seconds=4),
            ),
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=admin.id,
                event_type="ticket.assigned",
                resource_type="tenant_ticket",
                resource_id=str(ticket.id),
                details={"to": admin.id, "private": "operativo"},
                created_at=now + timedelta(seconds=5),
            ),
        ]
    )
    db.session.commit()

    public_response = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        headers=_auth_headers(app, citizen, tenant.slug),
    )
    assert public_response.status_code == 200, public_response.get_json()
    public_payload = public_response.get_json() or {}
    public_text = {item.get("preview_text") for item in public_payload.get("unified_conversation_stream") or []}
    assert public_text == {"fallback publico", "mensaje publico", "reply publico"}
    assert all(
        (item.get("payload") or {}).get("visibility") == "public"
        for item in public_payload.get("unified_conversation_stream") or []
    )

    internal_response = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        headers=_auth_headers(app, admin, tenant.slug),
    )
    assert internal_response.status_code == 200, internal_response.get_json()
    internal_payload = internal_response.get_json() or {}
    internal_text = {item.get("preview_text") for item in internal_payload.get("unified_conversation_stream") or []}
    assert {
        "fallback publico",
        "fallback interno",
        "mensaje publico",
        "mensaje interno",
        "reply publico",
        "reply interno",
        "ticket.assigned",
    } == internal_text
    audit_item = next(
        item
        for item in internal_payload.get("unified_conversation_stream") or []
        if (item.get("payload") or {}).get("event_type") == "ticket.assigned"
    )
    assert audit_item["stream_type"] == "evento"
    assert audit_item["payload"]["details"]["private"] == "operativo"


def test_v2_timeline_rejects_cursor_reuse_after_target_ticket_authorization(client, app):
    tenant_a, admin_a, citizen_a = _tenant_with_users("endpoint-cursor-a")
    ticket_a, conversation_a = _ticket_with_conversation(
        tenant_a,
        citizen_a,
        conversation_id="endpoint-cursor-a-conv",
    )
    tenant_b, admin_b, citizen_b = _tenant_with_users("endpoint-cursor-b")
    ticket_b, _conversation_b = _ticket_with_conversation(
        tenant_b,
        citizen_b,
        conversation_id="endpoint-cursor-b-conv",
    )
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    db.session.add_all(
        [
            Message(
                conversation_id=conversation_a.id,
                tenant_id=tenant_a.id,
                sender_type="user",
                direction="in",
                body=f"cursor-source-{index}",
                created_at=now + timedelta(seconds=index),
            )
            for index in range(3)
        ]
    )
    db.session.commit()

    source_response = client.get(
        f"/api/v2/tickets/{ticket_a.id}/timeline",
        query_string={"limit": 1},
        headers=_auth_headers(app, admin_a, tenant_a.slug),
    )
    assert source_response.status_code == 200, source_response.get_json()
    cursor = (source_response.get_json() or {}).get("next_cursor")
    assert cursor

    # Admin B is fully authorized for ticket B. Only after that authorization
    # succeeds should the cursor binding reject tenant/ticket reuse as 400.
    target_response = client.get(
        f"/api/v2/tickets/{ticket_b.id}/timeline",
        query_string={"limit": 1, "cursor": cursor},
        headers=_auth_headers(app, admin_b, tenant_b.slug),
    )
    assert target_response.status_code == 400, target_response.get_json()
    assert (target_response.get_json() or {}).get("reason_code") == "invalid_history_pagination"

    # An actor without access to ticket A still receives 404 before the cursor
    # is parsed, so a signed cursor cannot become a ticket-existence oracle.
    unauthorized_source = client.get(
        f"/api/v2/tickets/{ticket_a.id}/timeline",
        query_string={"limit": 1, "cursor": cursor},
        headers=_auth_headers(app, admin_b, tenant_b.slug),
    )
    assert unauthorized_source.status_code == 404
    assert (unauthorized_source.get_json() or {}).get("reason_code") == "ticket_not_found"


def test_v2_timeline_paginates_full_message_page_then_two_intake_attachments_and_dedupes_comment_copy(client, app):
    tenant, admin, citizen = _tenant_with_users("endpoint-attachment-page")
    attachments = [
        {
            "id": "intake-photo-1",
            "url": "https://cdn.example.test/intake-photo-1.jpg",
            "name": "luminaria-1.jpg",
            "mime_type": "image/jpeg",
        },
        {
            "id": "intake-photo-2",
            "url": "https://cdn.example.test/intake-photo-2.jpg",
            "name": "luminaria-2.jpg",
            "mime_type": "image/jpeg",
        },
    ]
    ticket, conversation = _ticket_with_conversation(
        tenant,
        citizen,
        conversation_id="endpoint-attachment-page-conv",
    )
    base_time = datetime(2026, 8, 13, 12, tzinfo=timezone.utc)
    ticket.created_at = base_time
    ticket.datos_extra = {**ticket.datos_extra, "attachments": attachments}
    db.session.add(ticket)
    db.session.bulk_save_objects(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user",
                direction="in",
                body=f"message-before-attachments-{index:02d}",
                meta_payload={"visibility": "public"},
                created_at=base_time + timedelta(seconds=index + 1),
            )
            for index in range(50)
        ]
    )
    db.session.commit()
    headers = _auth_headers(app, admin, tenant.slug)

    first = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        query_string={"limit": 50},
        headers=headers,
    )
    assert first.status_code == 200, first.get_json()
    first_payload = first.get_json() or {}
    first_items = first_payload.get("unified_conversation_stream") or []
    assert len(first_items) == 50
    assert first_payload.get("has_more") is True
    assert {item.get("source") for item in first_items} == {"conversation_message"}
    cursor = first_payload.get("next_cursor")
    assert cursor

    second = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        query_string={"limit": 50, "cursor": cursor},
        headers=headers,
    )
    assert second.status_code == 200, second.get_json()
    second_payload = second.get_json() or {}
    second_items = second_payload.get("unified_conversation_stream") or []
    assert len(second_items) == 2
    assert second_payload.get("has_more") is False
    assert second_payload.get("next_cursor") is None
    assert {item.get("source") for item in second_items} == {"ticket_attachment"}
    assert {item.get("stream_type") for item in second_items} == {"message"}
    assert {item["payload"]["attachmentInfo"]["id"] for item in second_items} == {
        "intake-photo-1",
        "intake-photo-2",
    }
    assert {item["id"] for item in first_items}.isdisjoint({item["id"] for item in second_items})

    # A legacy comment can contain the same intake attachment. The normalized
    # merge keeps one message for that file and suppresses the intake duplicate.
    duplicate_comment = {
        "id": "legacy-comment-with-photo-1",
        "body": "Foto recibida en la conversación",
        "visibility": "public",
        "created_at": (base_time + timedelta(milliseconds=1)).isoformat(),
        "attachmentInfo": attachments[0],
    }
    ticket.datos_extra = {**ticket.datos_extra, "comments": [duplicate_comment]}
    db.session.add(ticket)
    db.session.commit()

    merged = client.get(
        f"/api/v2/tickets/{ticket.id}/timeline",
        query_string={"limit": 100},
        headers=headers,
    )
    assert merged.status_code == 200, merged.get_json()
    merged_items = (merged.get_json() or {}).get("unified_conversation_stream") or []
    attachment_ids = [
        (item.get("payload") or {}).get("attachmentInfo", {}).get("id")
        for item in merged_items
        if (item.get("payload") or {}).get("attachmentInfo")
    ]
    assert attachment_ids.count("intake-photo-1") == 1
    assert attachment_ids.count("intake-photo-2") == 1
