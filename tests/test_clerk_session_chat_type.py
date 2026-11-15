"""The Clerk panel session takes its chat type from the organization, not a stale user column.

Production evidence (14/11): a government account created as ``pyme`` kept ``tipo_chat='pyme'``
after being linked to a ``municipio`` organization; analytics then asked for scope ``pyme`` and
the backend answered 400 "tenant context is invalid for scope". Synthetic identities only.
"""
import pytest

from models import TenantProfile, User, db
from services.clerk_auth_service import (
    build_chatboc_session_payload,
    complete_clerk_onboarding,
    upsert_user_from_clerk,
)
import jwt
from flask import current_app


def _claims(sub):
    return {"sub": sub, "sid": f"sess_{sub}", "sts": "active", "email": f"{sub}@chat-type.test", "email_verified": True}


def _profile(sub):
    return {
        "id": sub,
        "first_name": "Cuenta",
        "primary_email_address_id": "email_1",
        "email_addresses": [{"id": "email_1", "email_address": f"{sub}@chat-type.test", "verification": {"status": "verified"}}],
    }


def _organization(slug, tipo):
    owner = User(name=f"Owner {slug}", email=f"owner-{slug}@chat-type.test", rol="admin", tipo_chat=tipo)
    owner.set_password("owner-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Org {slug}",
        tipo=tipo,
        municipio_id=owner.id if tipo == "municipio" else None,
        pyme_id=owner.id if tipo != "municipio" else None,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    return tenant


def _session(sub, tenant, stored_chat_type):
    user = upsert_user_from_clerk(_claims(sub), _profile(sub), profile_is_trusted=True)
    user.rol = "admin"
    user.tipo_chat = stored_chat_type
    if tenant is not None:
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
    db.session.commit()
    complete_clerk_onboarding(user, {"terms_accepted": True, "terms_version": "2026-07-11"})
    return build_chatboc_session_payload(user, tenant, clerk_claims=_claims(sub), auth_intent="tenant_owner")


def _claim(token):
    return jwt.decode(token, current_app.config["SECRET_KEY"], algorithms=["HS256"])["tipo_chat"]


@pytest.mark.parametrize(
    "tipo,stored,expected",
    [("municipio", "pyme", "municipio"), ("gobierno", "pyme", "municipio"), ("pyme", "municipio", "pyme"), ("municipio", "municipio", "municipio")],
)
def test_panel_session_follows_the_organization_type(client, tipo, stored, expected):
    with client.application.app_context():
        tenant = _organization(f"chat-type-{tipo}-{stored}", tipo)
        payload = _session(f"user_chat_type_{tipo}_{stored}", tenant, stored)
        assert payload["user"]["tipo_chat"] == expected
        assert payload["user"]["tenant_slug"] == tenant.slug
        assert payload["token"]
        assert _claim(payload["token"]) == expected


def test_session_without_organization_keeps_the_stored_value(client):
    with client.application.app_context():
        user = upsert_user_from_clerk(_claims("user_chat_type_none"), _profile("user_chat_type_none"), profile_is_trusted=True)
        user.tipo_chat = "pyme"
        db.session.commit()
        payload = build_chatboc_session_payload(user, None, clerk_claims=_claims("user_chat_type_none"), auth_intent="tenant_owner")
        assert payload["user"]["tipo_chat"] == "pyme"
