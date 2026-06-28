from app import db
from models import MunicipioTicket, TenantProfile, User
from services.actions.municipio_actions import ConsultarEstadoTicketActionHandler


def _create_municipio(slug: str, email: str):
    owner = User(
        name=f"Municipio {slug}",
        email=email,
        rol="admin",
        tipo_chat="municipio",
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Municipio {slug}",
        tipo="municipio",
        municipio_id=owner.id,
        configuracion={"tenant_slug": slug, "slug": slug},
    )
    db.session.add(tenant)
    db.session.flush()
    return owner, tenant


def _create_ticket(tenant: TenantProfile, owner: User, ticket_number: str, pin: str, estado: str):
    ticket = MunicipioTicket(
        pregunta="consulta de estado",
        nro_ticket=ticket_number,
        consulta_pin=pin,
        estado=estado,
        categoria="Luminaria",
        tenant_id=tenant.id,
        municipio_id=owner.id,
    )
    db.session.add(ticket)
    db.session.flush()
    return ticket


def test_status_lookup_is_scoped_to_current_municipio(client):
    owner_a, tenant_a = _create_municipio("municipio-a", "municipio-a@test.com")
    owner_b, tenant_b = _create_municipio("municipio-b", "municipio-b@test.com")
    own_ticket = _create_ticket(tenant_a, owner_a, "881001", "111222", "en_proceso")
    other_ticket = _create_ticket(tenant_b, owner_b, "992002", "333444", "cerrado")
    db.session.commit()

    handler = ConsultarEstadoTicketActionHandler(
        {
            "user_obj": owner_a,
            "municipio_config_actual": {
                "tenant_slug": tenant_a.slug,
                "slug": tenant_a.slug,
            },
        }
    )

    own_result = handler.execute(
        {"id_ticket_mencionado": own_ticket.nro_ticket, "pin": own_ticket.consulta_pin}
    )
    assert own_result["success"] is True
    assert own_result["data"]["ticket_id"] == own_ticket.nro_ticket

    other_result = handler.execute(
        {"id_ticket_mencionado": other_ticket.nro_ticket, "pin": other_ticket.consulta_pin}
    )
    assert other_result["success"] is False
    assert "No encontr" in other_result["message_to_user"]


def test_status_lookup_without_municipio_scope_fails_closed(client):
    owner, tenant = _create_municipio("municipio-scope", "municipio-scope@test.com")
    ticket = _create_ticket(tenant, owner, "771001", "555666", "nuevo")
    db.session.commit()

    handler = ConsultarEstadoTicketActionHandler({})

    result = handler.execute(
        {"id_ticket_mencionado": ticket.nro_ticket, "pin": ticket.consulta_pin}
    )

    assert result["success"] is False
    assert "validar el municipio" in result["message_to_user"]
