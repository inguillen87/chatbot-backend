from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy import event

from models import Conversacion, MunicipioTicket, TenantProfile, TicketComentario, db
from services.ticket_service import servicio_tickets


def _seed_large_history(owner_user, *, count: int = 275):
    tenant = TenantProfile(
        slug="legacy-history-performance",
        nombre="Municipalidad historial performance",
        tipo="municipio",
        municipio_id=owner_user.id,
        is_active=True,
        configuracion={},
    )
    db.session.add(tenant)
    db.session.flush()
    owner_user.tenant_id = tenant.id
    owner_user.tenant_slug = tenant.slug
    owner_user.tipo_chat = "municipio"

    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=owner_user.id,
        user_id=owner_user.id,
        nro_ticket="M-HISTORY-PERFORMANCE",
        consulta_pin="HIS001",
        pregunta="Historial municipal de gran volumen",
        asunto="Prueba de continuidad",
        categoria="luminarias",
        estado="nuevo",
        canal_ingreso="whatsapp",
        datos_extra={},
    )
    db.session.add(ticket)
    db.session.flush()

    tied_at = datetime(2026, 8, 29, 18, tzinfo=timezone.utc)
    db.session.bulk_save_objects(
        [
            TicketComentario(
                municipio_ticket_id=ticket.id,
                comentario=f"perf-comment-{index:03d}",
                es_admin=bool(index % 2),
                origen="internal" if index == count else "chat",
                fecha=tied_at,
            )
            for index in range(1, count + 1)
        ]
    )
    db.session.commit()
    return tenant, ticket


def _headers(app, owner_user, tenant: TenantProfile) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": owner_user.id,
            "rol": owner_user.rol,
            "tenant_slug": tenant.slug,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant.slug,
    }


def test_legacy_municipal_reader_uses_one_bounded_comment_query(
    app,
    client,
    init_database,
    owner_user,
):
    _tenant, ticket = _seed_large_history(owner_user)
    statements: list[str] = []

    def record_sql(_connection, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(str(statement).lower().split())
        if " from ticket_comentario " in f" {normalized} ":
            statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", record_sql)
    try:
        timeline, historial = (
            servicio_tickets.obtener_historial_timeline_paginado_municipio(
                ticket,
                limit=30,
                cursor=None,
            )
        )
    finally:
        event.remove(db.engine, "before_cursor_execute", record_sql)

    assert len(statements) == 1
    assert " limit " in statements[0]
    assert len(historial) == 31
    # 31 bounded comments plus ticket-created/current-state projections.
    assert len(timeline) <= 33
    assert TicketComentario.query.filter_by(
        municipio_ticket_id=ticket.id
    ).count() == 275


def test_legacy_municipal_endpoint_keyset_continues_across_tied_large_history(
    app,
    client,
    init_database,
    owner_user,
):
    tenant, ticket = _seed_large_history(owner_user)
    headers = _headers(app, owner_user, tenant)
    cursor = None
    cursors: set[str] = set()
    item_ids: set[str] = set()
    comment_texts: list[str] = []
    page_sizes: list[int] = []

    for _page_number in range(12):
        query = {"limit": 40}
        if cursor:
            query["cursor"] = cursor
        response = client.get(
            f"/tickets/municipio/{ticket.id}/timeline",
            query_string=query,
            headers=headers,
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json() or {}
        items = payload.get("unified_conversation_stream") or []
        page_sizes.append(len(items))
        assert len(items) <= 40
        page_ids = {str(item["id"]) for item in items}
        assert item_ids.isdisjoint(page_ids)
        item_ids.update(page_ids)
        comment_texts.extend(
            str(item.get("preview_text"))
            for item in items
            if str(item.get("preview_text") or "").startswith("perf-comment-")
        )

        if not payload.get("has_more"):
            assert payload.get("next_cursor") is None
            break
        next_cursor = payload.get("next_cursor")
        assert next_cursor
        assert next_cursor not in cursors
        cursors.add(next_cursor)
        cursor = next_cursor
    else:  # pragma: no cover - protects against a non-advancing keyset
        raise AssertionError("legacy history cursor did not terminate")

    assert page_sizes[:6] == [40, 40, 40, 40, 40, 40]
    assert len(comment_texts) == 275
    assert len(set(comment_texts)) == 275
    assert set(comment_texts) == {
        f"perf-comment-{index:03d}" for index in range(1, 276)
    }
    # Authorized operators retain the pre-existing visibility semantics.
    assert "perf-comment-275" in comment_texts


def test_legacy_municipal_endpoint_bounds_and_continues_chatbot_conversation_source(
    app,
    client,
    init_database,
    owner_user,
):
    tenant, ticket = _seed_large_history(owner_user, count=0)
    ticket.anon_id = "legacy-history-conversation-session"
    tied_at = datetime(2026, 8, 29, 19, tzinfo=timezone.utc)
    db.session.bulk_save_objects(
        [
            Conversacion(
                session_id=ticket.anon_id,
                pregunta=f"conversation-question-{index:03d}",
                respuesta=f"conversation-response-{index:03d}",
                fuente="chat",
                timestamp=tied_at,
            )
            for index in range(1, 126)
        ]
    )
    db.session.add(ticket)
    db.session.commit()
    headers = _headers(app, owner_user, tenant)
    cursor = None
    seen_ids: set[str] = set()
    seen_text: set[str] = set()

    for _page_number in range(12):
        query = {"limit": 35}
        if cursor:
            query["cursor"] = cursor
        response = client.get(
            f"/tickets/municipio/{ticket.id}/timeline",
            query_string=query,
            headers=headers,
        )
        assert response.status_code == 200, response.get_json()
        payload = response.get_json() or {}
        items = payload.get("unified_conversation_stream") or []
        assert len(items) <= 35
        current_ids = {str(item["id"]) for item in items}
        assert seen_ids.isdisjoint(current_ids)
        seen_ids.update(current_ids)
        seen_text.update(
            str(item.get("preview_text"))
            for item in items
            if str(item.get("preview_text") or "").startswith("conversation-")
        )
        if not payload.get("has_more"):
            assert payload.get("next_cursor") is None
            break
        cursor = payload.get("next_cursor")
        assert cursor
    else:  # pragma: no cover
        raise AssertionError("conversation history cursor did not terminate")

    assert seen_text == {
        *{f"conversation-question-{index:03d}" for index in range(1, 126)},
        *{f"conversation-response-{index:03d}" for index in range(1, 126)},
    }
