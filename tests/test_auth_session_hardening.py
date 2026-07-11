from datetime import datetime, timedelta, timezone

import jwt

from database import db
from models import User


def _create_user(*, email: str = "session@test.com", role: str = "admin") -> User:
    user = User(name="Session User", email=email, rol=role, tipo_chat="pyme")
    user.set_password("safe-password")
    db.session.add(user)
    db.session.commit()
    return user


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


def test_v2_logout_clears_domain_and_host_only_token_cookies(client):
    client.application.config.update(
        SESSION_COOKIE_DOMAIN=".chatboc.ar",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="None",
    )

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
