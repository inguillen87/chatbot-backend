import jwt

from app import db
from models import MunicipioTicket, TenantProfile, TicketComentario, User


def _auth_headers(app, user: User, tenant_slug: str) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "tipo_chat": user.tipo_chat,
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant": tenant_slug,
        "X-Tenant-Slug": tenant_slug,
    }


def test_tenant_admin_can_read_public_ticket_conversation(client, app):
    owner = User(
        email="junin-owner-chat@test.com",
        name="Junin Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="junin-chat-scope",
        nombre="Municipalidad de Junin",
        tipo="municipio",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug

    ticket = MunicipioTicket(
        municipio_id=owner.id,
        tenant_id=tenant.id,
        nro_ticket="378430",
        pregunta="Arreglo de calle",
        categoria="Arreglo de calle",
        estado="nuevo",
        consulta_pin="900144",
    )
    db.session.add(ticket)
    db.session.flush()

    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario="hola que tal como va mi reclamo? puedo hablar con alguien en vivo?",
        es_admin=False,
    )
    db.session.add(comment)
    db.session.commit()

    headers = _auth_headers(app, owner, tenant.slug)
    query_string = {"tenant_slug": tenant.slug, "tenant": tenant.slug}

    messages_response = client.get(
        f"/tickets/chat/{ticket.id}/mensajes",
        headers=headers,
        query_string=query_string,
    )
    assert messages_response.status_code == 200
    messages = messages_response.get_json()["mensajes"]
    assert [item["texto"] for item in messages] == [comment.comentario]

    timeline_response = client.get(
        f"/tickets/municipio/{ticket.id}/timeline",
        headers=headers,
        query_string=query_string,
    )
    assert timeline_response.status_code == 200
    timeline_payload = timeline_response.get_json()
    assert any(
        item.get("preview_text") == comment.comentario
        for item in timeline_payload["unified_conversation_stream"]
    )

    read_state_response = client.post(
        f"/tickets/municipio/{ticket.id}/read-state",
        headers={**headers, "X-Chat-Session-Id": "admin-ticket-panel"},
        query_string=query_string,
        json={"last_read_comment_id": comment.id},
    )
    assert read_state_response.status_code == 200
    read_state_payload = read_state_response.get_json()
    assert read_state_payload["read_state"]["viewer_user_id"] == owner.id
    assert read_state_payload["read_state"]["last_read_comment_id"] == comment.id


def test_same_type_admin_cannot_read_other_tenant_ticket_conversation(client, app):
    owner_a = User(
        email="tenant-a-owner-chat@test.com",
        name="Tenant A Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner_a.set_password("pass")
    owner_b = User(
        email="tenant-b-owner-chat@test.com",
        name="Tenant B Owner",
        rol="admin",
        tipo_chat="municipio",
    )
    owner_b.set_password("pass")
    db.session.add_all([owner_a, owner_b])
    db.session.flush()

    tenant_a = TenantProfile(
        slug="tenant-a-chat-scope",
        nombre="Municipio A",
        tipo="municipio",
        municipio_id=owner_a.id,
    )
    tenant_b = TenantProfile(
        slug="tenant-b-chat-scope",
        nombre="Municipio B",
        tipo="municipio",
        municipio_id=owner_b.id,
    )
    db.session.add_all([tenant_a, tenant_b])
    db.session.flush()
    owner_a.tenant_id = tenant_a.id
    owner_a.tenant_slug = tenant_a.slug
    owner_b.tenant_id = tenant_b.id
    owner_b.tenant_slug = tenant_b.slug

    ticket_b = MunicipioTicket(
        municipio_id=owner_b.id,
        tenant_id=tenant_b.id,
        nro_ticket="B-378430",
        pregunta="Arreglo de calle en municipio B",
        categoria="Arreglo de calle",
        estado="nuevo",
        consulta_pin="900144",
    )
    db.session.add(ticket_b)
    db.session.flush()

    db.session.add(
        TicketComentario(
            municipio_ticket_id=ticket_b.id,
            comentario="mensaje privado del tenant B",
            es_admin=False,
        )
    )
    db.session.commit()

    headers = _auth_headers(app, owner_a, tenant_b.slug)
    query_string = {"tenant_slug": tenant_b.slug, "tenant": tenant_b.slug}

    messages_response = client.get(
        f"/tickets/chat/{ticket_b.id}/mensajes",
        headers=headers,
        query_string=query_string,
    )
    assert messages_response.status_code == 403

    timeline_response = client.get(
        f"/tickets/municipio/{ticket_b.id}/timeline",
        headers=headers,
        query_string=query_string,
    )
    assert timeline_response.status_code == 403
