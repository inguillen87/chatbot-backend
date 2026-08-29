from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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
from services.normalized_ticket_history import (
    MAX_LEGACY_FALLBACK_COMMENTS,
    NormalizedTicketHistoryCursorError,
    NormalizedTicketHistoryNotFound,
    read_normalized_ticket_history,
)


def _tenant(slug: str) -> tuple[TenantProfile, User]:
    owner = User(
        name=f"Owner {slug}",
        email=f"{slug}@example.test",
        rol="admin",
        tenant_slug=slug,
        tipo_chat="municipio",
    )
    owner.set_password("test-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.add(owner)
    return tenant, owner


def _ticket(
    tenant: TenantProfile,
    owner: User,
    *,
    conversation_id: str | None = None,
    comments: list[dict] | None = None,
) -> TenantTicket:
    extra: dict = {"title": "Historial normalizado"}
    if conversation_id is not None:
        extra["conversation_id"] = conversation_id
    if comments is not None:
        extra["comments"] = comments
    ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=owner.id,
        categoria="luminarias",
        descripcion="Luminaria apagada",
        estado="nuevo",
        origen="whatsapp",
        datos_extra=extra,
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def _conversation(tenant: TenantProfile, owner: User, *, conversation_id: str) -> Conversation:
    conversation = Conversation(
        id=conversation_id,
        tenant_id=tenant.id,
        created_by_user_id=owner.id,
        status="active",
    )
    db.session.add(conversation)
    db.session.flush()
    return conversation


def _read_all_ids(*, tenant_id: int, ticket_id: int, include_internal: bool, limit: int) -> list[str]:
    cursor = None
    pages: list[list[str]] = []
    for _ in range(30):
        page = read_normalized_ticket_history(
            tenant_id=tenant_id,
            ticket_id=ticket_id,
            include_internal=include_internal,
            limit=limit,
            cursor=cursor,
        )
        pages.append([item["id"] for item in page["items"]])
        pagination = page["pagination"]
        if not pagination["has_more"]:
            break
        cursor = pagination["next_cursor"]
        assert cursor
    else:  # pragma: no cover - protects against a cursor that never advances
        raise AssertionError("pagination did not terminate")

    # Older pages are prepended by the CRM; reconstruct that final ordering.
    ordered: list[str] = []
    for page_ids in pages:
        ordered = page_ids + ordered
    return ordered


def test_normalized_history_pages_more_than_one_thousand_messages_with_sql_keyset(client):
    tenant, owner = _tenant("history-thousand")
    conversation = _conversation(tenant, owner, conversation_id="history-thousand-conversation")
    ticket = _ticket(tenant, owner, conversation_id=conversation.id)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.session.bulk_save_objects(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user",
                direction="in",
                body=f"mensaje-{index:04d}",
                meta_payload={"visibility": "public"},
                created_at=start + timedelta(seconds=index),
            )
            for index in range(1005)
        ]
    )
    db.session.commit()

    ordered_ids = _read_all_ids(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=100,
    )

    assert len(ordered_ids) == 1005
    assert len(set(ordered_ids)) == 1005
    assert ordered_ids == [f"message:{row.id}" for row in Message.query.order_by(Message.created_at, Message.id)]


def test_same_timestamp_merge_is_stable_and_dedupes_canonical_reply_from_json_fallback(client):
    tenant, owner = _tenant("history-ties")
    conversation = _conversation(tenant, owner, conversation_id="history-ties-conversation")
    tied_at = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    duplicate_event_id = "reply-event-same-in-json"
    ticket = _ticket(
        tenant,
        owner,
        conversation_id=conversation.id,
        comments=[
            {
                "id": duplicate_event_id,
                "origin": "admin_panel",
                "action": "reply",
                "body": "respuesta persistida",
                "visibility": "public",
                "created_at": tied_at.isoformat(),
            },
            {
                "id": "legacy-only",
                "origin": "legacy",
                "action": "comment",
                "body": "solo compatibilidad",
                "visibility": "public",
                "created_at": tied_at.isoformat(),
            },
        ],
    )
    db.session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user",
                direction="in",
                body="mensaje uno",
                meta_payload={"visibility": "public"},
                created_at=tied_at,
            ),
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="assistant",
                direction="out",
                body="mensaje dos",
                meta_payload={"visibility": "public"},
                created_at=tied_at,
            ),
            TenantTicketReplyEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                event_id=duplicate_event_id,
                body="respuesta persistida",
                visibility="public",
                actor_user_id=owner.id,
                created_at=tied_at,
            ),
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=owner.id,
                event_type="ticket.status_changed",
                resource_type="tenant_ticket",
                resource_id=str(ticket.id),
                details={"from": "nuevo", "to": "en_proceso"},
                created_at=tied_at,
            ),
        ]
    )
    db.session.commit()

    first_order = _read_all_ids(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=True,
        limit=2,
    )
    second_order = _read_all_ids(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=True,
        limit=2,
    )

    assert first_order == second_order
    assert len(first_order) == 5
    assert len(set(first_order)) == 5
    assert not any(item_id == f"legacy:{duplicate_event_id}" for item_id in first_order)
    assert any(item_id.startswith("reply:") for item_id in first_order)


def test_public_scope_excludes_internal_replies_messages_audits_and_fallback(client):
    tenant, owner = _tenant("history-visibility")
    conversation = _conversation(tenant, owner, conversation_id="history-visibility-conversation")
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    ticket = _ticket(
        tenant,
        owner,
        conversation_id=conversation.id,
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
                meta_payload={"visibility": "internal", "private_note": "no filtrar"},
                created_at=now + timedelta(seconds=2),
            ),
            TenantTicketReplyEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                event_id="reply-public",
                body="reply publico",
                visibility="public",
                created_at=now + timedelta(seconds=3),
            ),
            TenantTicketReplyEvent(
                tenant_id=tenant.id,
                ticket_id=ticket.id,
                event_id="reply-internal",
                body="reply interno",
                visibility="internal",
                created_at=now + timedelta(seconds=4),
            ),
            AuditEvent(
                tenant_id=tenant.id,
                actor_user_id=owner.id,
                event_type="ticket.assigned",
                resource_type="tenant_ticket",
                resource_id=str(ticket.id),
                details={"visibility": "public", "to": owner.id, "private": "operativo"},
                created_at=now + timedelta(seconds=5),
            ),
        ]
    )
    db.session.commit()

    public_page = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=50,
    )
    public_bodies = {item["body"] for item in public_page["items"]}
    assert public_bodies == {"fallback publico", "mensaje publico", "reply publico"}
    assert all(item["visibility"] == "public" for item in public_page["items"])
    assert all("metadata" not in item for item in public_page["items"])

    internal_page = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=True,
        limit=50,
    )
    internal_bodies = {item["body"] for item in internal_page["items"]}
    assert {
        "fallback publico",
        "fallback interno",
        "mensaje publico",
        "mensaje interno",
        "reply publico",
        "reply interno",
        "ticket.assigned",
    } == internal_bodies
    internal_message = next(item for item in internal_page["items"] if item["body"] == "mensaje interno")
    assert internal_message["metadata"]["private_note"] == "no filtrar"


def test_ticket_and_conversation_reads_fail_closed_across_tenants(client):
    tenant_a, owner_a = _tenant("history-tenant-a")
    tenant_b, owner_b = _tenant("history-tenant-b")
    foreign_conversation = _conversation(
        tenant_b,
        owner_b,
        conversation_id="history-foreign-conversation",
    )
    ticket = _ticket(tenant_a, owner_a, conversation_id=foreign_conversation.id)
    db.session.add(
        Message(
            conversation_id=foreign_conversation.id,
            tenant_id=tenant_b.id,
            sender_type="user",
            direction="in",
            body="mensaje de otro tenant",
            created_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
    )
    db.session.commit()

    page = read_normalized_ticket_history(
        tenant_id=tenant_a.id,
        ticket_id=ticket.id,
        include_internal=True,
        limit=20,
    )
    assert page["items"] == []
    assert page["sources"]["conversation_message"]["requested_conversation_id"] == foreign_conversation.id
    assert page["sources"]["conversation_message"]["linked_conversation_id"] is None

    with pytest.raises(NormalizedTicketHistoryNotFound):
        read_normalized_ticket_history(
            tenant_id=tenant_b.id,
            ticket_id=ticket.id,
            include_internal=True,
        )


def test_missing_conversation_id_never_uses_a_synthetic_ticket_conversation(client):
    tenant, owner = _tenant("history-no-link")
    ticket = _ticket(tenant, owner)
    synthetic = _conversation(tenant, owner, conversation_id=f"ticket-{ticket.id}")
    db.session.add(
        Message(
            conversation_id=synthetic.id,
            tenant_id=tenant.id,
            sender_type="user",
            direction="in",
            body="no pertenece al ticket",
            created_at=datetime(2026, 8, 5, tzinfo=timezone.utc),
        )
    )
    db.session.commit()

    page = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
    )
    assert page["items"] == []
    assert page["sources"]["conversation_message"]["requested_conversation_id"] is None
    assert page["sources"]["conversation_message"]["linked_conversation_id"] is None


def test_legacy_json_fallback_is_hard_bounded_and_reports_discarded_rows(client):
    tenant, owner = _tenant("history-fallback-bound")
    start = datetime(2026, 8, 6, tzinfo=timezone.utc)
    comments = [
        {
            "id": index,
            "body": f"legacy-{index}",
            "visibility": "public",
            "created_at": (start + timedelta(seconds=index)).isoformat(),
        }
        for index in range(150)
    ]
    ticket = _ticket(tenant, owner, comments=comments)
    db.session.commit()

    page = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=100,
    )
    fallback = page["sources"]["legacy_fallback"]
    assert len(page["items"]) == MAX_LEGACY_FALLBACK_COMMENTS
    assert fallback["available"] == 150
    assert fallback["considered"] == MAX_LEGACY_FALLBACK_COMMENTS
    assert fallback["discarded_by_bound"] == 50
    assert {item["body"] for item in page["items"]} == {f"legacy-{index}" for index in range(50, 150)}


def test_fallback_cursor_survives_append_and_drop_of_oldest_bounded_comment(client):
    tenant, owner = _tenant("history-fallback-moving-window")
    start = datetime(2026, 8, 6, 12, tzinfo=timezone.utc)
    comments = [
        {
            # Deliberately no id: old rows can predate stable event ids.
            "body": f"moving-{index}",
            "visibility": "public",
            "created_at": (start + timedelta(seconds=index)).isoformat(),
            "actor": {"id": owner.id, "role": "agent"},
        }
        for index in range(MAX_LEGACY_FALLBACK_COMMENTS)
    ]
    ticket = _ticket(tenant, owner, comments=comments)
    db.session.commit()

    first = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=10,
    )
    first_bodies = [item["body"] for item in first["items"]]
    assert first_bodies == [f"moving-{index}" for index in range(90, 100)]
    cursor = first["pagination"]["next_cursor"]
    assert cursor

    # Simulate the compatibility writer maintaining a hard cap: append the
    # newest event and drop the oldest between requests.
    updated_comments = comments[1:] + [
        {
            "body": "moving-100",
            "visibility": "public",
            "created_at": (start + timedelta(seconds=100)).isoformat(),
            "actor": {"id": owner.id, "role": "agent"},
        }
    ]
    ticket.datos_extra = {**ticket.datos_extra, "comments": updated_comments}
    db.session.add(ticket)
    db.session.commit()

    second = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=10,
        cursor=cursor,
    )
    second_bodies = [item["body"] for item in second["items"]]
    assert second_bodies == [f"moving-{index}" for index in range(80, 90)]
    assert set(first_bodies).isdisjoint(second_bodies)


def test_cursor_rejects_tampering_scope_and_ticket_reuse(client):
    tenant, owner = _tenant("history-cursor")
    conversation = _conversation(tenant, owner, conversation_id="history-cursor-conversation")
    ticket = _ticket(tenant, owner, conversation_id=conversation.id)
    other_ticket = _ticket(tenant, owner, conversation_id=conversation.id)
    now = datetime(2026, 8, 7, tzinfo=timezone.utc)
    db.session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                sender_type="user",
                direction="in",
                body=f"cursor-{index}",
                created_at=now + timedelta(seconds=index),
            )
            for index in range(3)
        ]
    )
    db.session.commit()

    first = read_normalized_ticket_history(
        tenant_id=tenant.id,
        ticket_id=ticket.id,
        include_internal=False,
        limit=1,
    )
    cursor = first["pagination"]["next_cursor"]
    assert cursor

    with pytest.raises(NormalizedTicketHistoryCursorError):
        read_normalized_ticket_history(
            tenant_id=tenant.id,
            ticket_id=ticket.id,
            include_internal=False,
            limit=1,
            cursor=f"{cursor[:-1]}{'A' if cursor[-1] != 'A' else 'B'}",
        )
    with pytest.raises(NormalizedTicketHistoryCursorError):
        read_normalized_ticket_history(
            tenant_id=tenant.id,
            ticket_id=ticket.id,
            include_internal=True,
            limit=1,
            cursor=cursor,
        )
    with pytest.raises(NormalizedTicketHistoryCursorError):
        read_normalized_ticket_history(
            tenant_id=tenant.id,
            ticket_id=other_ticket.id,
            include_internal=False,
            limit=1,
            cursor=cursor,
        )
    with pytest.raises(NormalizedTicketHistoryCursorError):
        read_normalized_ticket_history(
            tenant_id=tenant.id,
            ticket_id=ticket.id,
            include_internal=False,
            limit=1,
            cursor="definitivamente-no-es-un-cursor",
        )
