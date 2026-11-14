"""Clerk panel access with an organization scope (own domain or /t/<slug>/login).

A scoped panel access is exclusive to that organization: an account from another one,
or one without an organization yet, never receives a Chatboc session there.
Synthetic identities only; Clerk verification is replaced in-process.
"""
import pytest

from models import AuthSession, TenantProfile, User, db
from services.clerk_auth_service import (
    ClerkAuthError,
    resolve_clerk_session_tenant,
    upsert_user_from_clerk,
)


def _claims(sub, email):
    return {"sub": sub, "sid": f"sess_{sub}", "sts": "active", "email": email, "email_verified": True}


def _profile(sub, email):
    return {
        "id": sub,
        "first_name": "Cuenta",
        "last_name": "Sintética",
        "primary_email_address_id": "email_1",
        "email_addresses": [{"id": "email_1", "email_address": email, "verification": {"status": "verified"}}],
    }


def _tenant(slug, tipo="municipio"):
    owner = User(name=f"Owner {slug}", email=f"owner-{slug}@chatboc.test", rol="admin", tipo_chat=tipo)
    owner.set_password("owner-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo=tipo,
        municipio_id=owner.id if tipo == "municipio" else None,
        pyme_id=owner.id if tipo != "municipio" else None,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _member(sub, email, tenant):
    user = upsert_user_from_clerk(_claims(sub, email), _profile(sub, email), profile_is_trusted=True)
    if tenant is not None:
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        user.rol = "admin"
    db.session.flush()
    return user


def _expect(code, status, **kwargs):
    with pytest.raises(ClerkAuthError) as raised:
        resolve_clerk_session_tenant(**kwargs)
    assert raised.value.reason_code == code
    assert raised.value.status_code == status


def test_member_enters_its_own_organization_scope(client):
    with client.application.app_context():
        tdf = _tenant("scope-tdf")
        user = _member("user_scope_member", "member@scope.test", tdf)
        for scope in ("scope-tdf", "  Scope-TDF "):
            resolved = resolve_clerk_session_tenant(user, auth_intent="tenant_owner", tenant_slug=scope)
            assert resolved.id == tdf.id


def test_account_of_another_organization_is_rejected(client):
    with client.application.app_context():
        _tenant("scope-tdf")
        other = _tenant("scope-other", tipo="pyme")
        user = _member("user_scope_foreign", "foreign@scope.test", other)
        _expect("organization_mismatch", 403, user=user, auth_intent="tenant_owner", tenant_slug="scope-tdf")


def test_account_without_organization_cannot_onboard_from_a_scoped_access(client):
    with client.application.app_context():
        _tenant("scope-tdf")
        user = _member("user_scope_new", "new@scope.test", None)
        _expect("organization_mismatch", 403, user=user, auth_intent="tenant_owner", tenant_slug="scope-tdf")


def test_unknown_organization_is_rejected_like_a_foreign_one(client):
    with client.application.app_context():
        tdf = _tenant("scope-tdf")
        user = _member("user_scope_unknown", "unknown@scope.test", tdf)
        _expect("organization_mismatch", 403, user=user, auth_intent="tenant_owner", tenant_slug="scope-inexistente")


def test_malformed_scope_is_rejected(client):
    with client.application.app_context():
        tdf = _tenant("scope-tdf")
        user = _member("user_scope_bad", "bad@scope.test", tdf)
        for scope in ("", "   ", "../tdf", "a" * 81, 7):
            _expect(
                "tenant_slug_required" if scope in ("", "   ", 7) else "tenant_slug_invalid",
                400,
                user=user,
                auth_intent="tenant_owner",
                tenant_slug=scope,
            )


def test_central_access_without_scope_is_unchanged(client):
    with client.application.app_context():
        other = _tenant("scope-other", tipo="pyme")
        user = _member("user_scope_central", "central@scope.test", other)
        assert resolve_clerk_session_tenant(user, auth_intent="tenant_owner").id == other.id
        newcomer = _member("user_scope_newcomer", "newcomer@scope.test", None)
        assert resolve_clerk_session_tenant(newcomer, auth_intent="tenant_owner") is None


def test_session_route_issues_nothing_for_a_foreign_account(client, monkeypatch):
    app = client.application
    with app.app_context():
        _tenant("scope-tdf")
        other = _tenant("scope-other", tipo="pyme")
        user = _member("user_scope_route", "route@scope.test", other)
        db.session.commit()
        user_id = user.id
    claims, profile = _claims("user_scope_route", "route@scope.test"), _profile("user_scope_route", "route@scope.test")
    monkeypatch.setattr("routes.auth.verify_clerk_session_token", lambda token: claims)
    monkeypatch.setattr("routes.auth.fetch_trusted_clerk_profile", lambda value: profile)
    monkeypatch.setitem(app.config, "CLERK_SESSION_RETURN_TOKEN", True)
    monkeypatch.setitem(app.config, "CLERK_SESSION_COOKIE_ENABLED", True)

    response = client.post(
        "/api/auth/clerk/session",
        json={"auth_intent": "tenant_owner", "tenant_slug": "scope-tdf"},
        headers={"Authorization": "Bearer synthetic.remote.clerk"},
    )

    assert response.status_code == 403, response.get_json()
    body = response.get_json()
    assert body.get("reason_code") == "organization_mismatch"
    assert "token" not in body and "user" not in body
    assert client.get_cookie("auth_token") is None
    with app.app_context():
        assert AuthSession.query.filter_by(actor_id=user_id).count() == 0
