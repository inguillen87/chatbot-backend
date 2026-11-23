import pytest

from models import ChatSessionContext, PymeTicket, Rubro, TenantProfile, User, db
from services.actions.general_actions import RegistrarUsuarioActionHandler


@pytest.fixture
def organization(client):
    owner = User(name="Synthetic owner", email="owner@example.test", password_hash="unused",
                 token="registration-owner", entity_token="registration-entity", tipo_chat="pyme")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="registration-test", nombre="Synthetic", tipo="pyme",
                           pyme_id=owner.id, plan="full")
    db.session.add(tenant)
    db.session.commit()
    return owner, tenant


def test_registration_uses_server_scope_and_visitor_consent(organization):
    owner, tenant = organization
    handler = RegistrarUsuarioActionHandler({
        "user_id": owner.id, "tenant_id": tenant.id, "empresa_token": owner.token,
        "registration_consent": {"acepta_marketing": "false", "acepto_terminos": True},
    })
    result = handler.execute({
        "name": "Visitor", "email": "visitor@example.test", "password": "sample",
        "acepta_marketing": True, "rol": "admin", "empresa_id": -1,
    })
    assert result["success"]
    visitor = db.session.get(User, result["data"]["user_id"])
    assert (visitor.empresa_id, visitor.tenant_id, visitor.tenant_slug) == (owner.id, tenant.id, tenant.slug)
    assert visitor.rol == "usuario" and visitor.check_password("sample")
    assert visitor.acepto_terminos and visitor.fecha_aceptacion_terminos
    assert not visitor.acepta_marketing and visitor.fecha_aceptacion_marketing is None


def test_registration_ignores_organization_and_consent_from_model(organization):
    owner, tenant = organization
    handler = RegistrarUsuarioActionHandler({
        "user_id": owner.id, "tenant_id": tenant.id, "empresa_token": owner.token,
    })
    result = handler.execute({
        "name": "Visitor", "email": "visitor@example.test", "password": "sample",
        "empresa_token": "unrelated", "acepto_terminos": True,
    })
    assert result["success"]
    visitor = db.session.get(User, result["data"]["user_id"])
    assert visitor.tenant_id == tenant.id
    assert not visitor.acepto_terminos and visitor.fecha_aceptacion_terminos is None


@pytest.mark.parametrize("state", ["inactive", "absent", "ambiguous", "wrong_scope"])
def test_registration_rejects_unresolved_organization(organization, state):
    owner, tenant = organization
    context = {"user_id": owner.id, "tenant_id": tenant.id, "empresa_token": owner.token}
    if state == "inactive":
        tenant.is_active = False
    elif state == "absent":
        db.session.delete(tenant)
    elif state == "ambiguous":
        db.session.add(TenantProfile(slug="other", nombre="Other", tipo="pyme", pyme_id=owner.id, plan="full"))
    else:
        context["tenant_id"] = -1
    db.session.commit()
    result = RegistrarUsuarioActionHandler(context).execute({
        "name": "Visitor", "email": "visitor@example.test", "password": "sample",
    })
    assert not result["success"]
    assert not User.query.filter_by(email="visitor@example.test").first()


def test_legacy_lookup_never_provisions_company_or_sector(organization):
    from services.pymes import get_or_create_pyme_user_by_token

    owner, tenant = organization
    counts = User.query.count(), TenantProfile.query.count(), Rubro.query.count()
    assert get_or_create_pyme_user_by_token("unknown-registration-token") is None
    assert (User.query.count(), TenantProfile.query.count(), Rubro.query.count()) == counts
    assert get_or_create_pyme_user_by_token(owner.entity_token) == owner
    tenant.configuracion = {"widget_tokens": ["published-registration"]}
    db.session.commit()
    assert get_or_create_pyme_user_by_token("published-registration") == owner
    assert get_or_create_pyme_user_by_token("published") is None
    tenant.is_active = False
    db.session.commit()
    assert get_or_create_pyme_user_by_token(owner.token) is None


def test_widget_gate_preserves_hosted_chat_registration(organization):
    owner, tenant = organization
    tenant.plan = "free"
    db.session.commit()
    context = {"user_id": owner.id, "tenant_id": tenant.id}
    data = {"name": "Visitor", "email": "native@example.test", "password": "sample"}
    assert not RegistrarUsuarioActionHandler({**context, "widget_registration": True}).execute(data)["success"]
    assert RegistrarUsuarioActionHandler(context).execute(data)["success"]


@pytest.mark.parametrize("path", ["/auth/register", "/auth/chatuserregisterpanel"])
def test_register_selectors_must_agree(organization, client, path):
    owner, tenant = organization
    other = User(name="Other owner", email="other@example.test", password_hash="unused", token="other-owner")
    db.session.add(other)
    db.session.flush()
    other_tenant = TenantProfile(slug="other-org", nombre="Other", tipo="pyme", pyme_id=other.id, plan="full")
    db.session.add(other_tenant)
    db.session.commit()
    data = {"name": "Visitor", "email": "new@example.test", "password": "sample",
            "empresa_token": owner.token, "tenant_slug": other_tenant.slug}
    assert client.post(path, json=data).status_code == 409
    data["tenant_slug"] = tenant.slug
    assert client.post(path, json=data, headers={"X-Entity-Token": other.token}).status_code == 409
    assert User.query.filter_by(email=data["email"]).first() is None

    # A legacy owner value and a published widget value must identify the same organization.
    other_tenant.configuracion = {"widget_tokens": [owner.token]}
    db.session.commit()
    assert client.post(path, json=data).status_code == 404
    assert User.query.filter_by(email=data["email"]).first() is None


def test_widget_register_alias_checks_bound_tenant_and_consent(organization, client):
    owner, tenant = organization
    payload = {"name": "Visitor", "email": "visitor@example.test", "password": "sample",
               "empresa_token": owner.token, "tenant_slug": tenant.slug, "acepta_marketing": "false",
               "acepto_terminos": True}
    response = client.post("/auth/register", json=payload,
                           headers={"X-Entity-Token": owner.entity_token})
    assert response.status_code == 201
    visitor = User.query.filter_by(email=payload["email"]).one()
    assert (visitor.empresa_id, visitor.tenant_id) == (owner.id, tenant.id)
    assert not visitor.acepta_marketing and visitor.fecha_aceptacion_marketing is None
    assert visitor.acepto_terminos and visitor.fecha_aceptacion_terminos
    login = {"empresa_token": owner.entity_token, "email": visitor.email, "password": "different"}
    assert client.post("/auth/chatuserloginpanel", json=login).status_code == 401
    login["password"] = "sample"
    logged_in = client.post("/auth/chatuserloginpanel", json=login)
    assert logged_in.status_code == 200 and logged_in.get_json()["id"] == visitor.id


def test_registration_keeps_anonymous_context_with_its_visitor(organization, client):
    owner, tenant = organization
    session = ChatSessionContext(tenant_id=tenant.id, anon_id="first-visitor",
                                 chat_session_id="registration-session", context_data={})
    db.session.add(session)
    db.session.commit()
    resp = client.post("/auth/chatuserregisterpanel", json={
        "name": "Visitor", "email": "visitor@example.test", "password": "sample",
        "empresa_token": owner.token,
    }, headers={"X-Chat-Session-Id": session.chat_session_id, "X-Anon-Id": "second-visitor"})
    assert resp.status_code in (201, 409)
    db.session.refresh(session)
    assert session.anon_id == "first-visitor" and session.user_id is None


@pytest.mark.parametrize("path", ["/auth/chatuserloginpanel", "/auth/widget/login"])
def test_login_adopts_only_proven_anonymous_history(organization, client, path):
    from utils.auth_helpers import generar_token

    owner, tenant = organization
    visitor = User(name="Visitor", email="history@example.test", empresa_id=owner.id, tenant_id=tenant.id)
    visitor.set_password("sample")
    session = ChatSessionContext(chat_session_id="saved-chat", anon_id="saved-anon", tenant_id=tenant.id)
    ticket = PymeTicket(tenant_id=tenant.id, anon_id="saved-anon", pregunta="Local test", nro_ticket=101)
    db.session.add_all([visitor, session, ticket])
    db.session.commit()
    data = {"empresa_token": owner.token, "email": visitor.email, "password": "sample",
            "anon_id": session.anon_id}
    headers = {}
    if path == "/auth/widget/login":
        token = generar_token(owner.id, owner.rol, owner.tipo_chat, None, None)
        headers["Authorization"] = f"Bearer {token}"
    assert client.post(path, json=data, headers=headers).status_code == 200
    db.session.refresh(ticket)
    assert ticket.user_id is None
    headers["X-Chat-Session-Id"] = session.chat_session_id
    assert client.post(path, json=data, headers=headers).status_code == 200
    db.session.refresh(ticket)
    db.session.refresh(session)
    assert ticket.user_id == session.user_id == visitor.id
