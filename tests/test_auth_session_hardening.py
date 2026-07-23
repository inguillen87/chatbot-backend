from datetime import datetime, timedelta, timezone

import jwt
import pytest

from database import db
from models import TenantProfile, User
from utils.auth_helpers import auth_session_version, generar_token


def _create_user(*, email: str = "session@test.com", role: str = "admin") -> User:
    user = User(name="Session User", email=email, rol=role, tipo_chat="pyme")
    user.set_password("safe-password")
    db.session.add(user)
    db.session.commit()
    return user


def _clerk_token(app, user: User, *, sid: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "user_id": user.id,
            "rol": user.rol,
            "auth_provider": "clerk",
            "session_kind": "clerk",
            "sid": sid,
            "clerk_sid": sid,
            "jti": f"jti-{sid}",
            "sv": auth_session_version(user),
            "tenant_slug": user.tenant_slug,
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )


def _mark_clerk_managed(user: User) -> None:
    user.accesibilidad = {
        "auth": {
            "provider": "clerk",
            "session_version": 1,
            "clerk": {"user_id": f"user_{user.id}"},
        }
    }


def test_v2_refresh_rejects_clerk_and_demo_sessions(client):
    with client.application.app_context():
        user = _create_user()
        now = datetime.now(timezone.utc)
        clerk_token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "auth_provider": "clerk",
                "session_kind": "clerk",
                "sid": "sess_test",
                "clerk_sid": "sess_test",
                "jti": "jti_test",
                "sv": 1,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            client.application.config["SECRET_KEY"],
            algorithm="HS256",
        )

    response = client.post("/api/v2/auth/refresh", json={"token": clerk_token})

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "provider_resync_required"


def test_legacy_refresh_cannot_extend_account_after_clerk_migration(client):
    with client.application.app_context():
        user = _create_user(email="migrated-refresh@test.com")
        now = datetime.now(timezone.utc)
        legacy_token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            client.application.config["SECRET_KEY"],
            algorithm="HS256",
        )
        user.accesibilidad = {
            "auth": {
                "provider": "clerk",
                "session_version": 1,
                "clerk": {"user_id": "user_migrated_refresh"},
            }
        }
        db.session.commit()

    response = client.post(
        "/auth/refresh",
        headers={"Authorization": f"Bearer {legacy_token}"},
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "provider_resync_required"


def test_clerk_managed_login_fails_closed_when_clerk_is_disabled(client, monkeypatch):
    monkeypatch.setenv("CLERK_DISABLED", "true")
    monkeypatch.delenv("CLERK_ENABLED", raising=False)
    with client.application.app_context():
        user = _create_user(email="clerk-disabled@test.com")
        user.accesibilidad = {
            "auth": {
                "provider": "clerk",
                "session_version": 1,
                "clerk": {"user_id": "user_clerk_disabled"},
            }
        }
        db.session.commit()

    for endpoint in ("/auth/login", "/auth/admin/login"):
        response = client.post(
            endpoint,
            json={"email": "clerk-disabled@test.com", "password": "safe-password"},
        )
        assert response.status_code == 403
        assert response.get_json()["reason_code"] == "clerk_required"


@pytest.mark.parametrize("endpoint", ("/auth/login", "/auth/admin/login"))
def test_legacy_login_rejects_inactive_tenant_without_disclosing_state(client, endpoint):
    with client.application.app_context():
        user = _create_user(email=f"inactive-{endpoint.rsplit('/', 1)[-1]}@test.com")
        tenant = TenantProfile(
            slug=f"inactive-{endpoint.rsplit('/', 1)[-1]}",
            nombre="Inactive Tenant",
            tipo="pyme",
            pyme_id=user.id,
            is_active=False,
        )
        db.session.add(tenant)
        db.session.flush()
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        db.session.commit()
        email = user.email

    response = client.post(
        endpoint,
        json={"email": email, "password": "safe-password"},
    )

    assert response.status_code == 401
    payload = response.get_json()
    expected_error = (
        "Email o contraseña incorrectos."
        if endpoint == "/auth/login"
        else "Credenciales inválidas"
    )
    assert payload == {"error": expected_error}


def test_token_required_rejects_inactive_tenant_flask_session_cookie(client):
    with client.application.app_context():
        user = _create_user(email="inactive-cookie@test.com")
        tenant = TenantProfile(
            slug="inactive-cookie",
            nombre="Inactive Cookie Tenant",
            tipo="pyme",
            pyme_id=user.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        user.tenant_id = tenant.id
        user.tenant_slug = tenant.slug
        db.session.commit()
        user_id = user.id

    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(user_id)
        flask_session["_fresh"] = True

    with client.application.app_context():
        tenant = TenantProfile.query.filter_by(slug="inactive-cookie").one()
        tenant.is_active = False
        db.session.commit()

    response = client.get("/auth/token-info")

    assert response.status_code == 401
    payload = response.get_json()
    assert payload["reason_code"] == "token_expired"
    assert payload["error"]["message"] == "Token inválido o sesión expirada"
    assert "tenant" not in str(payload).lower()


def test_explicit_bearer_identity_overrides_stale_flask_session(client):
    with client.application.app_context():
        stale_user = _create_user(email="stale-session@test.com")
        bearer_user = _create_user(email="fresh-bearer@test.com", role="empleado")
        token = generar_token(
            bearer_user.id,
            bearer_user.rol,
            bearer_user.tipo_chat,
            bearer_user.municipio_id,
            bearer_user.pyme_id,
        )
        stale_user_id = stale_user.id
        bearer_user_id = bearer_user.id

    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(stale_user_id)
        flask_session["_fresh"] = True

    response = client.get(
        "/auth/token-info",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["id"] == bearer_user_id
    assert payload["rol"] == "empleado"


@pytest.mark.parametrize(
    "bearer_value",
    ("invalid.jwt.value", "opaque-invalid-token", ""),
)
def test_invalid_explicit_bearer_does_not_fall_back_to_flask_session(
    client,
    bearer_value,
):
    with client.application.app_context():
        session_user = _create_user(email="invalid-bearer-session@test.com")
        session_user_id = session_user.id

    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(session_user_id)
        flask_session["_fresh"] = True

    response = client.get(
        "/auth/token-info",
        headers={"Authorization": f"Bearer {bearer_value}".rstrip()},
    )

    assert response.status_code == 401
    assert response.get_json()["reason_code"] == "token_expired"


def test_explicit_static_widget_bearer_overrides_stale_flask_session(client):
    with client.application.app_context():
        stale_user = _create_user(email="stale-static-session@test.com")
        widget_owner = _create_user(email="explicit-static-widget@test.com")
        widget_owner.entity_token = "explicit-static-widget-token"
        db.session.commit()
        stale_user_id = stale_user.id
        widget_owner_id = widget_owner.id

    with client.session_transaction() as flask_session:
        flask_session["_user_id"] = str(stale_user_id)
        flask_session["_fresh"] = True

    response = client.get(
        "/auth/token-info",
        headers={"Authorization": "Bearer explicit-static-widget-token"},
    )

    assert response.status_code == 200
    assert response.get_json()["id"] == widget_owner_id


@pytest.mark.parametrize("deactivation_method", ("delete", "put"))
def test_deactivating_tenant_revokes_related_clerk_tokens_after_reactivation(
    client,
    deactivation_method,
):
    with client.application.app_context():
        superadmin = _create_user(
            email="guillen.marce@gmail.com",
            role="super_admin",
        )
        _mark_clerk_managed(superadmin)

        owner = _create_user(email=f"owner-{deactivation_method}@test.com")
        employee = _create_user(
            email=f"employee-{deactivation_method}@test.com",
            role="empleado",
        )
        direct_member = _create_user(
            email=f"member-{deactivation_method}@test.com",
            role="usuario",
        )
        for user in (owner, employee, direct_member):
            _mark_clerk_managed(user)

        tenant = TenantProfile(
            slug=f"revoke-{deactivation_method}",
            nombre="Revocation Tenant",
            tipo="pyme",
            pyme_id=owner.id,
            is_active=True,
        )
        db.session.add(tenant)
        db.session.flush()
        owner.tenant_id = tenant.id
        owner.tenant_slug = tenant.slug
        employee.empresa_id = owner.id
        employee.tenant_slug = tenant.slug
        direct_member.tenant_id = tenant.id
        direct_member.tenant_slug = tenant.slug
        db.session.commit()

        employee_id = employee.id
        related_user_ids = (owner.id, employee.id, direct_member.id)
        prior_employee_token = _clerk_token(
            client.application,
            employee,
            sid=f"employee-{deactivation_method}",
        )
        superadmin_token = _clerk_token(
            client.application,
            superadmin,
            sid=f"superadmin-{deactivation_method}",
        )
        tenant_slug = tenant.slug

    employee_headers = {"Authorization": f"Bearer {prior_employee_token}"}
    admin_headers = {"Authorization": f"Bearer {superadmin_token}"}
    assert client.get("/auth/token-info", headers=employee_headers).status_code == 200

    tenant_url = f"/api/admin/tenants/{tenant_slug}"
    if deactivation_method == "delete":
        deactivation = client.delete(tenant_url, headers=admin_headers)
    else:
        deactivation = client.put(
            tenant_url,
            headers=admin_headers,
            json={"is_active": False},
        )
    assert deactivation.status_code == 200

    activation = client.post(f"{tenant_url}/activate", headers=admin_headers)
    assert activation.status_code == 200

    with client.application.app_context():
        related_users = [db.session.get(User, user_id) for user_id in related_user_ids]
        assert [auth_session_version(user) for user in related_users] == [2, 2, 2]
        employee = db.session.get(User, employee_id)
        replacement_token = _clerk_token(
            client.application,
            employee,
            sid=f"employee-replacement-{deactivation_method}",
        )

    revoked = client.get("/auth/token-info", headers=employee_headers)
    assert revoked.status_code == 401
    revoked_payload = revoked.get_json()
    assert revoked_payload["reason_code"] == "token_expired"
    assert revoked_payload["error"]["message"] == "Token inválido o sesión expirada"
    assert "tenant" not in str(revoked_payload).lower()

    replacement_headers = {"Authorization": f"Bearer {replacement_token}"}
    assert client.get("/auth/token-info", headers=replacement_headers).status_code == 200


def test_superadmin_login_fails_closed_when_clerk_is_disabled(client, monkeypatch):
    monkeypatch.setenv("CLERK_DISABLED", "true")
    monkeypatch.delenv("CLERK_ENABLED", raising=False)
    with client.application.app_context():
        _create_user(email="guillen.marce@gmail.com", role="super_admin")

    for endpoint in ("/auth/login", "/auth/admin/login"):
        response = client.post(
            endpoint,
            json={"email": "guillen.marce@gmail.com", "password": "safe-password"},
        )
        assert response.status_code == 403
        assert response.get_json()["reason_code"] == "clerk_required"


def test_v2_logout_clears_flask_and_token_cookies(client):
    with client.application.app_context():
        _create_user(email="logout@test.com")

    login = client.post(
        "/auth/login",
        json={"email": "logout@test.com", "password": "safe-password"},
    )
    assert login.status_code == 200

    logout = client.post("/api/v2/auth/logout")
    assert logout.status_code == 200
    cookies = "\n".join(logout.headers.getlist("Set-Cookie"))
    assert "auth_token=;" in cookies
    assert "widget_token=;" in cookies

    protected = client.get("/auth/me/dashboard")
    assert protected.status_code == 401


def test_v2_logout_clears_domain_and_host_only_token_cookies(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_DOMAIN", ".chatboc.ar")
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SECURE", True)
    monkeypatch.setitem(client.application.config, "SESSION_COOKIE_SAMESITE", "None")

    response = client.post("/api/v2/auth/logout")

    assert response.status_code == 200
    cookies = response.headers.getlist("Set-Cookie")
    assert any("auth_token=;" in value and "Domain=chatboc.ar" in value for value in cookies)
    assert any("auth_token=;" in value and "Domain=" not in value for value in cookies)
    assert any("widget_token=;" in value and "Domain=chatboc.ar" in value for value in cookies)


def test_legacy_google_never_issues_token_before_terms(client, monkeypatch):
    with client.application.app_context():
        user = _create_user(email="google-terms@test.com", role="usuario")
        user.acepto_terminos = False
        db.session.commit()
        user_id = user.id

    def _fake_google_login(*_args, **_kwargs):
        return User.query.get(user_id)

    monkeypatch.setattr("routes.auth.login_o_crear_usuario", _fake_google_login)

    response = client.post("/auth/google-login", json={"id_token": "verified-by-test"})

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["reason_code"] == "terms_required"
    assert payload["token"] is None
