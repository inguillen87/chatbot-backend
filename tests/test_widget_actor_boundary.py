from datetime import datetime, timedelta, timezone

import pytest
from flask import g

from extensions import db
from models import TenantProfile, User
from services.auth_session_lifecycle import issue_token
from utils.auth_helpers import (
    _generate_widget_session_token, anon_o_token_requerido, generar_token,
    token_requerido, user_from_token, widget_owner_from_token,
)


@pytest.fixture
def widget(client):
    owner = User(name="Owner", email="organization@example.invalid", rol="admin", tipo_chat="pyme",
                 password_hash="unused", token="widget-contract-static")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(slug="widget-contract", nombre="Organization", tipo="pyme", pyme_id=owner.id)
    db.session.add(tenant)
    db.session.commit()
    # A previously issued widget credential still carries the historical owner role.
    token = issue_token({
        "user_id": owner.id, "rol": owner.rol, "session_kind": "widget",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    })
    return owner, token


def test_widget_never_resolves_as_authenticated_actor(widget):
    owner, token = widget
    assert user_from_token(token) is None
    assert user_from_token(owner.token) is None


def test_new_widget_is_bound_to_organization_without_operator_role(widget):
    owner, _ = widget
    token, claims = _generate_widget_session_token(owner)
    assert claims["audience"] == "widget"
    assert claims["tenant_id"] == TenantProfile.query.filter_by(slug="widget-contract").one().id
    assert "rol" not in claims
    assert widget_owner_from_token(token).id == owner.id
    assert user_from_token(token) is None


@pytest.mark.parametrize("source", ["Authorization", "X-Entity-Token"])
def test_widget_does_not_inherit_cookie_viewer(app, widget, monkeypatch, source):
    import flask_login
    from routes.analytics_routes import _analytics_request_user
    from routes.encuestas_public import _resolve_preview_user
    from routes.productos import _resolve_authenticated_user

    owner, widget_token = widget
    value = f"Bearer {widget_token}" if source == "Authorization" else owner.token
    with app.test_request_context("/auth/me", headers={source: value}):
        monkeypatch.setattr(flask_login, "current_user", owner)
        app.preprocess_request()
        assert g.viewer is None
        assert _analytics_request_user() is None
        assert _resolve_preview_user() is None
        assert _resolve_authenticated_user() is None


def test_public_chat_distinguishes_widget_owner_from_visitor(app, widget):
    owner, token = widget
    with app.test_request_context("/ask", method="POST", headers={"Authorization": f"Bearer {token}"}):
        @anon_o_token_requerido
        def public_chat(current_user, owner_user, anon_id):
            assert anon_id
            assert owner_user.id == owner.id
            assert current_user is None
            assert g.jwt_user is None
            return "ok"

        assert public_chat().status_code == 200


def test_widget_market_cart_is_scoped_to_visitor(app, widget):
    from routes.market import _get_or_create_cart_for_user
    owner, _ = widget
    tenant = TenantProfile.query.filter_by(slug="widget-contract").one()
    for visitor in ("visitor-a", "visitor-b"):
        with app.test_request_context("/api/market/widget-contract/cart", headers={"X-Anon-Id": visitor}):
            g.widget_session = True
            cart = _get_or_create_cart_for_user(tenant, owner)
            assert cart.user_id is None
            assert cart.contact_email is None
            assert cart.session_id == visitor
            assert _get_or_create_cart_for_user(tenant, owner).id == cart.id


def test_market_keeps_authenticated_shopper_with_organization_header(app, widget):
    from routes.market import _get_or_create_cart_for_user
    owner, _ = widget
    tenant = TenantProfile.query.filter_by(slug="widget-contract").one()
    shopper = User(name="Buyer", email="buyer@example.invalid", rol="usuario", empresa_id=owner.id,
                   tenant_id=tenant.id, password_hash="unused")
    db.session.add(shopper)
    db.session.commit()
    token = generar_token(shopper.id, shopper.rol, "pyme", None, owner.id)
    with app.test_request_context("/api/market/widget-contract/cart", headers={
        "Authorization": f"Bearer {token}", "X-Entity-Token": owner.token, "X-Anon-Id": "shopper",
    }):
        @token_requerido
        def cart_view(user):
            assert not g.widget_session
            assert user.id == shopper.id
            cart = _get_or_create_cart_for_user(tenant, user)
            assert cart.user_id == shopper.id
            return "ok"

        assert cart_view().status_code == 200
