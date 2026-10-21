from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app import db
from models import TenantProfile, User
from models_memory import Contact


def _headers(app, actor: User, tenant: TenantProfile) -> dict[str, str]:
    token = jwt.encode(
        {"user_id": actor.id, "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "X-Tenant": tenant.slug}


def _user(email: str, name: str, *, role: str = "usuario", phone: str | None = None) -> User:
    user = User(email=email, name=name, rol=role, tipo_chat="pyme", telefono=phone)
    user.set_password("pass")
    return user


def _seed_directory():
    suffix = uuid4().hex[:8]
    admin_a = _user(f"admin-a-{suffix}@test.com", "Admin A", role="admin")
    admin_b = _user(f"admin-b-{suffix}@test.com", "Admin B", role="admin")
    db.session.add_all([admin_a, admin_b])
    db.session.flush()
    tenant_a = TenantProfile(slug=f"people-a-{suffix}", nombre="People A", tipo="pyme", pyme_id=admin_a.id)
    tenant_b = TenantProfile(slug=f"people-b-{suffix}", nombre="People B", tipo="pyme", pyme_id=admin_b.id)
    db.session.add_all([tenant_a, tenant_b])
    db.session.flush()
    admin_a.tenant_id = tenant_a.id
    admin_a.tenant_slug = tenant_a.slug
    admin_b.tenant_id = tenant_b.id
    admin_b.tenant_slug = tenant_b.slug
    db.session.commit()
    return admin_a, admin_b, tenant_a, tenant_b


def _contact(tenant, name, phone, *, email=None, channel="whatsapp", marketing=False, seen=None, **preferences):
    contact = Contact(
        id=str(uuid4()), tenant_id=tenant.id, name=name, phone=phone, email=email,
        type="neighbor", last_interaction_at=seen,
        preferences={"channel": channel, "marketing_opt_in": marketing, **preferences},
    )
    db.session.add(contact)
    return contact


def test_people_directory_masks_by_default_and_requires_existing_permission(client, app):
    admin, _, tenant, _ = _seed_directory()
    _contact(tenant, "Marcelo Vecino", "+5492613001234", email="marcelo@example.com")
    db.session.commit()

    masked = client.get("/api/v2/crm/people", headers=_headers(app, admin, tenant)).get_json()
    assert masked["contract_version"] == "crm.people.directory.v2"
    assert masked["pii"]["masked"] is True
    assert masked["items"][0]["name"] == "M*** V***"
    assert masked["items"][0]["phone"] == "***1234"
    assert masked["items"][0]["email"] == "m***@example.com"

    denied = client.get("/api/v2/crm/people?pii=full", headers=_headers(app, admin, tenant)).get_json()
    assert denied["pii"]["granted"] is False
    assert denied["pii"]["reason_code"] == "pii_permission_required"
    assert denied["items"][0]["phone"] == "***1234"

    admin.accesibilidad = {"employee_scope": {"permisos": ["crm_contacts_pii_read"]}}
    db.session.commit()
    full = client.get("/api/v2/crm/people?pii=full", headers=_headers(app, admin, tenant)).get_json()
    assert full["pii"]["granted"] is True
    assert full["items"][0]["name"] == "Marcelo Vecino"
    assert full["items"][0]["phone"] == "+5492613001234"


def test_people_directory_dedupes_before_stable_keyset_pagination(client, app):
    admin, _, tenant, _ = _seed_directory()
    now = datetime.now(timezone.utc)
    legacy = _user(f"vecino-{uuid4().hex[:8]}@test.com", "Vecino Duplicado", phone="+5492613111111")
    legacy.empresa_id = admin.id
    legacy.tenant_id = tenant.id
    db.session.add(legacy)
    db.session.flush()
    merged = _contact(
        tenant, "Vecino Actualizado", "+5492613111111", seen=now,
        legacy_user_id=legacy.id,
    )
    _contact(tenant, "Segundo", "+5492613222222", seen=now - timedelta(minutes=1))
    _contact(tenant, "Tercero", "+5492613333333", seen=now - timedelta(minutes=2))
    db.session.commit()

    first_response = client.get("/api/v2/crm/people?limit=2", headers=_headers(app, admin, tenant))
    assert first_response.status_code == 200
    first = first_response.get_json()
    assert first["page"]["total"] == 3
    assert first["page"]["has_more"] is True
    assert first["items"][0]["source"] == "user_contact"
    assert first["items"][0]["id"].startswith("person_")
    assert "contact_id" not in first["items"][0]
    assert "user_id" not in first["items"][0]
    assert "tags" not in first["items"][0]

    second = client.get(
        "/api/v2/crm/people",
        query_string={"limit": 2, "cursor": first["page"]["next_cursor"]},
        headers=_headers(app, admin, tenant),
    ).get_json()
    assert second["page"]["total"] == 3
    assert second["page"]["has_more"] is False
    assert {item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"])


def test_people_directory_cursor_rejects_tampering_filter_and_actor_mismatch(client, app):
    admin, _, tenant, _ = _seed_directory()
    now = datetime.now(timezone.utc)
    for index in range(3):
        _contact(tenant, f"Persona {index}", f"+549261400000{index}", seen=now - timedelta(minutes=index))
    manager = _user(f"manager-{uuid4().hex[:8]}@test.com", "Manager", role="manager")
    manager.tenant_id = tenant.id
    manager.tenant_slug = tenant.slug
    db.session.add(manager)
    db.session.commit()

    page = client.get("/api/v2/crm/people?limit=1", headers=_headers(app, admin, tenant)).get_json()
    cursor = page["page"]["next_cursor"]
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    invalid = client.get("/api/v2/crm/people", query_string={"cursor": tampered}, headers=_headers(app, admin, tenant))
    assert invalid.status_code == 400
    assert invalid.get_json()["reason_code"] == "invalid_people_cursor"

    mismatch = client.get(
        "/api/v2/crm/people", query_string={"cursor": cursor, "channel": "email"},
        headers=_headers(app, admin, tenant),
    )
    assert mismatch.status_code == 400
    assert mismatch.get_json()["reason_code"] == "people_cursor_filter_mismatch"

    actor_mismatch = client.get(
        "/api/v2/crm/people", query_string={"cursor": cursor}, headers=_headers(app, manager, tenant),
    )
    assert actor_mismatch.status_code == 400
    assert actor_mismatch.get_json()["reason_code"] == "invalid_people_cursor"


def test_people_directory_is_tenant_safe_and_filters_server_side(client, app):
    admin_a, admin_b, tenant_a, tenant_b = _seed_directory()
    _contact(tenant_a, "WhatsApp Optin", "+5492615000001", channel="whatsapp", marketing=True)
    _contact(tenant_a, "Email No", None, email="email-only@example.com", channel="email", marketing=False)
    _contact(tenant_b, "Secreto Otro Tenant", "+5492615999999", channel="whatsapp", marketing=True)
    db.session.commit()

    response = client.get(
        "/api/v2/crm/people",
        query_string={"q": "WhatsApp", "marketing": "true", "channel": "whatsapp"},
        headers=_headers(app, admin_a, tenant_a),
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["page"]["total"] == 1
    assert payload["filters"] == {
        "q": "WhatsApp", "marketing": "true", "channel": "whatsapp", "sort": "recent_desc"
    }

    forbidden = client.get("/api/v2/crm/people", headers=_headers(app, admin_a, tenant_b))
    assert forbidden.status_code == 403
    assert forbidden.get_json()["reason_code"] == "tenant_access_denied"

    own_b = client.get("/api/v2/crm/people?pii=full", headers=_headers(app, admin_b, tenant_b)).get_json()
    assert own_b["page"]["total"] == 1
    assert own_b["items"][0]["phone"] == "***9999"


def test_people_directory_excludes_owner_employee_and_conflicting_tenant_user(client, app):
    admin_a, _, tenant_a, tenant_b = _seed_directory()
    employee = _user(f"employee-{uuid4().hex[:8]}@test.com", "Empleado Interno", role="empleado")
    employee.empresa_id = admin_a.id
    employee.es_empleado = True
    conflicting = _user(f"conflict-{uuid4().hex[:8]}@test.com", "Usuario Tenant B")
    conflicting.empresa_id = admin_a.id
    conflicting.tenant_id = tenant_b.id
    legacy_client = _user(f"legacy-{uuid4().hex[:8]}@test.com", "Cliente Legacy")
    legacy_client.empresa_id = admin_a.id
    db.session.add_all([employee, conflicting, legacy_client])
    db.session.commit()

    payload = client.get("/api/v2/crm/people?pii=full", headers=_headers(app, admin_a, tenant_a)).get_json()
    assert payload["page"]["total"] == 1
    assert payload["items"][0]["name"] == "C*** L***"
    assert payload["pii"]["granted"] is False


def test_people_directory_never_merges_phone_or_email_without_explicit_binding(client, app):
    admin, _, tenant, _ = _seed_directory()
    shared_phone = "+5492615666777"
    user = _user(f"same-{uuid4().hex[:8]}@test.com", "Usuario Sin Binding", phone=shared_phone)
    user.tenant_id = tenant.id
    db.session.add(user)
    _contact(tenant, "Contacto Sin Binding", shared_phone)
    db.session.commit()

    payload = client.get("/api/v2/crm/people", headers=_headers(app, admin, tenant)).get_json()
    assert payload["page"]["total"] == 2
    assert [item["possible_duplicate"] for item in payload["items"]] == [True, True]
    assert {item["source"] for item in payload["items"]} == {"user", "contact"}


def test_people_directory_sensitive_search_requires_pii_permission(client, app):
    admin, _, tenant, _ = _seed_directory()
    _contact(tenant, "Nombre Publico", "+5492615888123", email="sensible@example.com")
    db.session.commit()

    masked_phone = client.get("/api/v2/crm/people?q=888123", headers=_headers(app, admin, tenant)).get_json()
    masked_email = client.get("/api/v2/crm/people?q=sensible@example.com", headers=_headers(app, admin, tenant)).get_json()
    assert masked_phone["page"]["total"] == 0
    assert masked_email["page"]["total"] == 0

    admin.accesibilidad = {"employee_scope": {"permissions": ["crm_contacts_pii_read"]}}
    db.session.commit()
    full = client.get(
        "/api/v2/crm/people", query_string={"q": "888123", "pii": "full"},
        headers=_headers(app, admin, tenant),
    ).get_json()
    assert full["page"]["total"] == 1
    assert full["pii"]["granted"] is True
    assert full["items"][0]["phone"] == "+5492615888123"
