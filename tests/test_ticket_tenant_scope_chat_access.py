import jwt
from types import SimpleNamespace

from app import db
from models import MunicipioTicket, PymeTicket, Rubro, TenantProfile, TicketComentario, User
from routes.ticket import _ticket_matches_tenant_scope


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


def test_explicit_foreign_tenant_id_never_falls_back_to_legacy_owner_scope():
    tenant = SimpleNamespace(id=10)
    mismatched_ticket = SimpleNamespace(tenant_id=20, municipio_id=910, pyme_id=77)

    assert not _ticket_matches_tenant_scope(mismatched_ticket, tenant, 910, None)
    assert not _ticket_matches_tenant_scope(mismatched_ticket, tenant, None, 77)

    legacy_ticket = SimpleNamespace(tenant_id=None, municipio_id=910, pyme_id=None)
    assert _ticket_matches_tenant_scope(legacy_ticket, tenant, 910, None)


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


def test_same_rubro_pyme_admin_cannot_read_or_reply_to_other_tenant_ticket(client, app):
    rubro = Rubro(clave="shared-rubro-http", nombre="Shared rubro")
    owner_a = User(
        email="pyme-a-owner@test.com",
        name="Pyme A",
        rol="admin",
        tipo_chat="pyme",
    )
    owner_b = User(
        email="pyme-b-owner@test.com",
        name="Pyme B",
        rol="admin",
        tipo_chat="pyme",
    )
    owner_a.set_password("pass")
    owner_b.set_password("pass")
    db.session.add_all([rubro, owner_a, owner_b])
    db.session.flush()
    owner_a.rubro_id = rubro.id
    owner_b.rubro_id = rubro.id

    tenant_a = TenantProfile(
        slug="pyme-a-chat-scope",
        nombre="Pyme A",
        tipo="pyme",
        pyme_id=owner_a.id,
    )
    tenant_b = TenantProfile(
        slug="pyme-b-chat-scope",
        nombre="Pyme B",
        tipo="pyme",
        pyme_id=owner_b.id,
    )
    db.session.add_all([tenant_a, tenant_b])
    db.session.flush()
    owner_a.tenant_id = tenant_a.id
    owner_a.tenant_slug = tenant_a.slug
    owner_b.tenant_id = tenant_b.id
    owner_b.tenant_slug = tenant_b.slug

    ticket_b = PymeTicket(
        tenant_id=tenant_b.id,
        rubro_id=rubro.id,
        nro_ticket=778899,
        pregunta="Pedido privado de Pyme B",
        estado="nuevo",
        user_id=owner_b.id,
    )
    db.session.add(ticket_b)
    db.session.commit()

    headers = _auth_headers(app, owner_a, tenant_b.slug)
    query_string = {"tenant_slug": tenant_b.slug, "tenant": tenant_b.slug}

    detail_response = client.get(
        f"/tickets/pyme/{ticket_b.id}",
        headers=headers,
        query_string=query_string,
    )
    assert detail_response.status_code == 403

    reply_response = client.post(
        f"/tickets/pyme/{ticket_b.id}/responder",
        headers=headers,
        query_string=query_string,
        json={"comentario": "Intento cross-tenant"},
    )
    assert reply_response.status_code == 403
