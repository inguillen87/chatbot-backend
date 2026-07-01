from models import User, Rubro, TenantProfile, db
import jwt
import pytest
from io import BytesIO
from datetime import datetime, timedelta
from flask import current_app
from utils.auth_helpers import anon_o_token_requerido

def test_perfil_alias_works(client):
    """Verifica que el alias /perfil funciona correctamente."""
    # Asegúrate de que exista un Rubro para asociar al usuario
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(email="perfil_alias@test.com", name="Perfil Alias", token="perfil-alias-token", rubro_id=rubro.id)
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    # Generate a JWT token for the user
    jwt_payload = {
        'user_id': user.id,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")

    response = client.get(
        '/perfil',
        headers={"Authorization": f"Bearer {jwt_token}"}
    )

    assert response.status_code == 200
    json_data = response.get_json()
    assert json_data["email"] == "perfil_alias@test.com"
    normalized_token = jwt_token.decode("utf-8") if isinstance(jwt_token, bytes) else jwt_token
    assert json_data["token"] == normalized_token
    assert json_data["auth_token"] == normalized_token
    assert json_data["entity_token"] == "perfil-alias-token"
    assert json_data["session_token"] == normalized_token
    assert json_data["session_kind"] == "panel"
    assert json_data["widget_session_active"] is False
    assert json_data["widget_embed_token"] is None
    assert json_data["widget_embed_token_kind"] == "plan_required"
    assert json_data["integration_access"]["enabled"] is False
    assert json_data["owner_token"] == "perfil-alias-token"
    assert json_data["widget_token_cookie_name"] == client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    assert json_data["session_expires_at"]
    assert "session_renew_until" not in json_data


def test_perfil_uses_tenant_slug_and_exposes_profile_sections(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="profile-sections@test.com",
        name="Profile Sections",
        token="profile-sections-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
        rol="admin",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="profile-sections-tenant",
        nombre="Profile Sections Tenant",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    jwt_payload = {"user_id": owner.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.get("/auth/perfil", headers={"Authorization": f"Bearer {jwt_token}"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["tenant_slug"] == tenant.slug
    assert data["tenantSlug"] == tenant.slug
    assert data["rubro_id"] == rubro.id
    assert data["rubro_clave"] == rubro.clave
    assert "tickets" in (data.get("profile_sections") or [])
    assert data["requires_rubro_selection"] is False


@pytest.mark.parametrize("role", ["tenant_admin", "admin_municipio", "admin_pyme"])
def test_admin_role_aliases_receive_ticket_panels_and_capabilities(client, role):
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="municipio", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email=f"{role}@panel.test",
        name=f"{role} Panel",
        token=f"{role}-panel-token",
        rubro_id=rubro.id,
        tipo_chat="municipio",
        rol=role,
        municipio_id=9001,
    )
    user.set_password("secret123")
    db.session.add(user)
    db.session.flush()

    tenant = TenantProfile(
        slug=f"{role.replace('_', '-')}-tenant",
        nombre=f"{role} Tenant",
        tipo="municipio",
        municipio_id=user.id,
    )
    db.session.add(tenant)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")
    headers = {"Authorization": f"Bearer {jwt_token}"}

    profile_response = client.get("/auth/perfil", headers=headers)
    assert profile_response.status_code == 200
    profile_payload = profile_response.get_json()
    assert "tickets" in (profile_payload.get("profile_sections") or [])
    assert "tickets.read" in (profile_payload.get("capabilities") or [])
    assert "crm.tickets.read" in (profile_payload.get("permissions") or [])

    bootstrap_response = client.get("/auth/session/bootstrap", headers=headers)
    assert bootstrap_response.status_code == 200
    bootstrap_payload = bootstrap_response.get_json()
    assert "tickets" in (bootstrap_payload.get("ui", {}).get("panels") or [])
    assert "tickets.read" in (bootstrap_payload.get("user", {}).get("capabilities") or [])

    dashboard_response = client.get("/auth/me/dashboard", headers=headers)
    assert dashboard_response.status_code == 200
    dashboard_payload = dashboard_response.get_json()
    assert "tickets" in (dashboard_payload.get("panels") or [])
    assert "tickets.read" in (dashboard_payload.get("capabilities") or [])

    login_response = client.post(
        "/auth/admin/login",
        json={"email": user.email, "password": "secret123"},
    )
    assert login_response.status_code == 200
    login_payload = login_response.get_json()
    assert login_payload["user"]["rol"] == role
    assert "tickets.read" in (login_payload["user"].get("capabilities") or [])


def test_perfil_allows_consent_avatar_url_and_exposes_picture_alias(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-profile@test.com",
        name="Avatar Profile",
        token="avatar-profile-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    avatar_url = "https://cdn.example.com/profiles/avatar-profile.jpg"
    update_response = client.put(
        "/auth/perfil",
        json={
            "avatar_url": avatar_url,
            "avatar_source": "profile_upload",
            "avatar_consent": True,
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert update_response.status_code == 200

    db.session.refresh(user)
    assert user.accesibilidad["identity"]["avatar_url"] == avatar_url
    assert user.accesibilidad["identity"]["avatar_source"] == "profile_upload"
    assert user.accesibilidad["identity"]["avatar_consent"] is True

    response = client.get("/auth/perfil", headers={"Authorization": f"Bearer {jwt_token}"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["avatar_url"] == avatar_url
    assert data["picture"] == avatar_url
    assert data["avatar_source"] == "profile_upload"
    assert data["avatar_consent"] is True
    assert data["profile_picture_consent"] is True
    assert data["identity"]["avatar_url"] == avatar_url
    assert data["identity"]["avatar_source"] == "profile_upload"
    assert data["identity"]["avatar_consent"] is True
    assert data["identity"]["fallback"] == "deterministic_identity_avatar"
    assert data["identity"]["policy"] == "consented_upload_or_social_only"
    assert "whatsapp_profile" in data["identity"]["blocked_sources"]


def test_perfil_rejects_avatar_url_without_explicit_consent(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-no-consent@test.com",
        name="Avatar No Consent",
        token="avatar-no-consent-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.put(
        "/auth/perfil",
        json={
            "avatar_url": "https://cdn.example.com/profiles/no-consent.jpg",
            "avatar_source": "profile_upload",
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 400
    assert "consentimiento" in response.get_json()["error"].lower()

    db.session.refresh(user)
    assert not user.accesibilidad


def test_perfil_hides_legacy_avatar_without_explicit_consent(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="legacy-avatar@test.com",
        name="Legacy Avatar",
        token="legacy-avatar-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    user.accesibilidad = {
        "identity": {
            "avatar_url": "https://cdn.example.com/profiles/legacy.jpg",
            "avatar_source": "profile_upload",
        }
    }
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.get("/auth/perfil", headers={"Authorization": f"Bearer {jwt_token}"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["avatar_url"] is None
    assert data["picture"] is None
    assert data["avatar_source"] is None
    assert data["avatar_consent"] is False
    assert data["profile_picture_consent"] is False
    assert data["identity"]["avatar_url"] is None
    assert data["identity"]["avatar_consent"] is False


def test_perfil_hides_legacy_avatar_with_unsafe_url_even_with_consent(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="legacy-unsafe-avatar@test.com",
        name="Legacy Unsafe Avatar",
        token="legacy-unsafe-avatar-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    user.accesibilidad = {
        "identity": {
            "avatar_url": "javascript:alert(1)",
            "avatar_source": "profile_upload",
            "avatar_consent": True,
        }
    }
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.get("/auth/perfil", headers={"Authorization": f"Bearer {jwt_token}"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["avatar_url"] is None
    assert data["picture"] is None
    assert data["avatar_source"] is None
    assert data["avatar_consent"] is False
    assert data["identity"]["avatar_url"] is None
    assert data["identity"]["avatar_consent"] is False


def test_perfil_rejects_unsafe_avatar_url(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="unsafe-avatar@test.com",
        name="Unsafe Avatar",
        token="unsafe-avatar-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.put(
        "/auth/perfil",
        json={"avatar_url": "javascript:alert(1)"},
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 400

    db.session.refresh(user)
    assert not user.accesibilidad


def test_perfil_rejects_scraped_avatar_source_even_with_https(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="scraped-avatar@test.com",
        name="Scraped Avatar",
        token="scraped-avatar-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.put(
        "/auth/perfil",
        json={
            "avatar_url": "https://cdn.example.com/profiles/scraped.jpg",
            "avatar_source": "whatsapp_scraped_unconsented",
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 400

    db.session.refresh(user)
    assert not user.accesibilidad


def test_perfil_rejects_whatsapp_profile_avatar_source_even_with_consent(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="whatsapp-profile-avatar@test.com",
        name="WhatsApp Profile Avatar",
        token="whatsapp-profile-avatar-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.put(
        "/auth/perfil",
        json={
            "avatar_url": "https://cdn.example.com/profiles/wa-profile.jpg",
            "avatar_source": "whatsapp_profile",
            "avatar_consent": True,
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 400

    db.session.refresh(user)
    assert not user.accesibilidad


def test_perfil_avatar_consent_false_clears_existing_avatar(client):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-clear@test.com",
        name="Avatar Clear",
        token="avatar-clear-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    user.accesibilidad = {
        "identity": {
            "avatar_url": "https://cdn.example.com/profiles/existing.jpg",
            "avatar_source": "profile_upload",
            "avatar_consent": True,
        }
    }
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.put(
        "/auth/perfil",
        json={
            "avatar_url": "https://cdn.example.com/profiles/should-not-save.jpg",
            "avatar_source": "profile_upload",
            "avatar_consent": False,
        },
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 200

    db.session.refresh(user)
    assert "avatar_url" not in user.accesibilidad["identity"]


def test_profile_avatar_upload_stores_consent_metadata(client, monkeypatch):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-upload@test.com",
        name="Avatar Upload",
        token="avatar-upload-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    def fake_upload(file_storage, kind="attachments"):
        assert kind == "profile_avatars"
        assert file_storage.mimetype == "image/png"
        return {
            "original_url": "https://cdn.example.com/profile_avatars/avatar.png",
            "original_name": file_storage.filename,
            "mimetype": file_storage.mimetype,
            "size": 12,
            "thumbUrl": "https://cdn.example.com/profile_avatars/avatar-thumb.webp",
        }

    monkeypatch.setattr("routes.auth.upload_to_gcs", fake_upload)

    response = client.post(
        "/auth/profile/avatar",
        data={
            "avatar": (BytesIO(b"fakepngbytes"), "avatar.png"),
            "avatar_consent": "true",
        },
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )
    assert response.status_code == 201
    payload = response.get_json()
    assert payload["avatar_url"] == "https://cdn.example.com/profile_avatars/avatar.png"
    assert payload["picture"] == payload["avatar_url"]
    assert payload["avatar_source"] == "profile_upload"
    assert payload["avatar_consent"] is True
    assert payload["identity"]["avatar_url"] == payload["avatar_url"]
    assert payload["identity"]["policy"] == "consented_upload_or_social_only"
    assert payload["identity"]["avatar_policy"] == "consented_upload_or_social_only"
    assert payload["identity"]["consent_required"] is True
    assert "profile_upload" in payload["identity"]["allowed_sources"]
    assert "whatsapp_profile" in payload["identity"]["blocked_sources"]
    assert payload["upload"]["thumb_url"] == "https://cdn.example.com/profile_avatars/avatar-thumb.webp"

    db.session.refresh(user)
    identity = user.accesibilidad["identity"]
    assert identity["avatar_url"] == payload["avatar_url"]
    assert identity["avatar_source"] == "profile_upload"
    assert identity["avatar_consent"] is True


def test_profile_avatar_upload_requires_explicit_consent(client, monkeypatch):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-missing-consent@test.com",
        name="Avatar Missing Consent",
        token="avatar-missing-consent-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    upload_called = False

    def fake_upload(file_storage, kind="attachments"):
        nonlocal upload_called
        upload_called = True
        return {"original_url": "https://cdn.example.com/profile_avatars/missing-consent.png"}

    monkeypatch.setattr("routes.auth.upload_to_gcs", fake_upload)

    response = client.post(
        "/auth/profile/avatar",
        data={"avatar": (BytesIO(b"fakepngbytes"), "avatar.png")},
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )

    assert response.status_code == 400
    assert upload_called is False
    db.session.refresh(user)
    assert not user.accesibilidad


def test_profile_avatar_upload_rejects_denied_consent(client, monkeypatch):
    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    user = User(
        email="avatar-denied@test.com",
        name="Avatar Denied",
        token="avatar-denied-token",
        rubro_id=rubro.id,
    )
    user.set_password("pw")
    db.session.add(user)
    db.session.commit()

    jwt_payload = {"user_id": user.id, "exp": datetime.utcnow() + timedelta(days=1)}
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    upload_called = False

    def fake_upload(file_storage, kind="attachments"):
        nonlocal upload_called
        upload_called = True
        return {"original_url": "https://cdn.example.com/profile_avatars/denied.png"}

    monkeypatch.setattr("routes.auth.upload_to_gcs", fake_upload)

    response = client.post(
        "/auth/profile/avatar",
        data={
            "avatar": (BytesIO(b"fakepngbytes"), "avatar.png"),
            "avatar_consent": "false",
        },
        content_type="multipart/form-data",
        headers={"Authorization": f"Bearer {jwt_token}"},
    )

    assert response.status_code == 400
    assert upload_called is False
    db.session.refresh(user)
    assert not user.accesibilidad


def test_perfil_returns_owner_token_for_employee(client):
    """Employees should receive the owner's static token for integrations."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="owner-employee@test.com",
        name="Owner", token="owner-employee-static-token",
        rol="admin", rubro_id=rubro.id, tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="owner-employee-full",
        nombre="Owner Employee Full",
        tipo="pyme",
        plan="full",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.add(owner)
    db.session.commit()

    employee = User(
        email="employee@test.com",
        name="Employee",
        rol="empleado",
        empresa_id=owner.id,
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    employee.set_password("pw")
    db.session.add(employee)
    db.session.commit()

    jwt_payload = {
        'user_id': employee.id,
        'exp': datetime.utcnow() + timedelta(days=1)
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config['SECRET_KEY'], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode('utf-8')

    response = client.get(
        '/auth/perfil',
        headers={"Authorization": f"Bearer {jwt_token}"}
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["session_token"] == jwt_token
    assert data["session_kind"] == "panel"
    assert data["entity_token"] == owner.token
    assert data["widget_embed_token"] == owner.token
    assert data["owner_token"] == owner.token


def test_auth_demo_assigns_rubro_for_first_time_demo_user(client):
    client.application.config["ENABLE_DEMO_MODE"] = True

    rubro = Rubro.query.filter_by(clave="ferreteria").first()
    if not rubro:
        rubro = Rubro(nombre="Ferretería", clave="ferreteria", es_publico=False)
        db.session.add(rubro)
        db.session.flush()

    owner = User(
        email="owner-ferreteria-demo@test.com",
        name="Owner Demo Ferretería",
        token="owner-ferreteria-demo-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
        rol="admin",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="ferreteria",
        nombre="Ferretería Demo",
        tipo="pyme",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.post("/auth/demo", json={"tenant_slug": tenant.slug})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["tenant_slug"] == tenant.slug
    assert payload["rubro_id"] == rubro.id
    assert payload["rubro"] == rubro.clave

    demo_user = User.query.filter_by(email=f"demo.{tenant.slug}@chatboc.ar").first()
    assert demo_user is not None
    assert demo_user.rubro_id == rubro.id


def test_perfil_accepts_static_entity_token_and_sets_widget_session(client):
    """Static entity tokens should bootstrap a widget session without manual renewal."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="static-token@test.com",
        name="Widget Owner",
        token="static-owner-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.flush()

    tenant = TenantProfile(
        slug="static-token-full",
        nombre="Static Token Full",
        tipo="pyme",
        plan="full",
        pyme_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.add(owner)
    db.session.commit()

    # Primer llamado con el token estático: debe emitir un JWT de sesión de widget.
    response = client.get('/auth/perfil', query_string={'token': owner.token})
    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    auth_token = data["auth_token"]
    assert auth_token and auth_token != owner.token
    assert auth_token.count('.') == 2
    assert data["widget_embed_token"] is None
    assert data["widget_embed_token_kind"] == "plan_required"
    assert data["owner_token"] == owner.token
    assert data["session_token"] == auth_token
    assert data["session_kind"] == "widget"
    assert data["widget_session_active"] is True
    assert data["session_expires_at"]
    assert data["session_renew_until"]
    assert data["widget_token_cookie_name"] == client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")

    widget_cookie_name = client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    cookie_headers = response.headers.getlist("Set-Cookie")
    assert any(
        cookie_header.startswith(f"{widget_cookie_name}=") and auth_token in cookie_header
        for cookie_header in cookie_headers
    )

    # Segundo llamado: el backend debe reutilizar el token de la cookie en lugar de emitir uno nuevo.
    response_2 = client.get('/auth/perfil', query_string={'token': owner.token})
    assert response_2.status_code == 200
    data_2 = response_2.get_json()
    assert data_2["auth_token"] == auth_token
    assert data_2["session_kind"] == "widget"
    assert data_2["session_token"] == auth_token
    assert data_2["widget_session_active"] is True

    # El token de widget no debe permitir acceder a rutas administrativas como /pedidos.
    forbidden = client.get('/pedidos', headers={'Authorization': f'Bearer {auth_token}'})
    assert forbidden.status_code == 403


def test_perfil_prefers_entity_header_over_placeholder_authorization(client):
    """If the widget sends a placeholder Authorization header, prefer the entity token header."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="placeholder-auth@test.com",
        name="Placeholder Owner",
        token="static-owner-from-header",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    response = client.get(
        '/auth/perfil',
        headers={
            'Authorization': 'Bearer demo-anon',
            'X-Entity-Token': owner.token,
        }
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    auth_token = data["auth_token"]
    assert auth_token and auth_token != owner.token
    assert auth_token.count('.') == 2


def test_static_entity_token_overrides_authorization_jwt(client):
    """A fresh entity token should override a stale Authorization JWT from another owner."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner_a = User(
        email="auth-jwt-a@test.com",
        name="Owner A",
        token="static-owner-a", 
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner_a.set_password("pw")

    owner_b = User(
        email="auth-jwt-b@test.com",
        name="Owner B",
        token="static-owner-b",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner_b.set_password("pw")

    db.session.add_all([owner_a, owner_b])
    db.session.commit()

    first = client.get('/auth/perfil', query_string={'token': owner_a.token})
    assert first.status_code == 200
    first_data = first.get_json()
    jwt_owner_a = first_data["auth_token"]
    assert jwt_owner_a and jwt_owner_a.count('.') == 2

    second = client.get(
        '/auth/perfil',
        headers={
            'Authorization': f'Bearer {jwt_owner_a}',
            'X-Entity-Token': owner_b.token,
        },
    )

    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data["entity_token"] == owner_b.token
    jwt_owner_b = second_data["auth_token"]
    assert jwt_owner_b and jwt_owner_b.count('.') == 2
    assert jwt_owner_b != jwt_owner_a

    widget_cookie_name = client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    cookie_headers = second.headers.getlist("Set-Cookie")
    assert any(
        cookie_header.startswith(f"{widget_cookie_name}=") and jwt_owner_b in cookie_header
        for cookie_header in cookie_headers
    )


def test_perfil_accepts_demo_anon_token(client):
    """The legacy 'demo-anon' token should resolve to the default municipio owner."""

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="municipio", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="demo-anon-owner@test.com",
        name="Demo Owner",
        rol="admin",
        token="demo-municipio-owner-token",
        rubro_id=rubro.id,
        tipo_chat="municipio",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    response = client.get('/auth/perfil', query_string={'token': 'demo-anon'})

    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    assert data["widget_embed_token"] is None
    assert data["widget_embed_token_kind"] == "plan_required"
    assert data["owner_token"] == owner.token
    assert data["session_kind"] == "widget"
    assert data["widget_session_active"] is True
    assert data["session_token"] and data["session_token"].count('.') == 2


def test_demo_anon_token_still_works_without_registry(monkeypatch, client):
    """If the demo registry is empty, fallback heuristics must still resolve demo-anon."""

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipalidad", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="fallback-demo-owner@test.com",
        name="Fallback Demo Owner",
        rol="admin",
        token="fallback-demo-owner-token",
        rubro_id=rubro.id,
        tipo_chat="municipio",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    monkeypatch.setattr("utils.auth_helpers.demo_rubro_for_token", lambda *_: None)

    response = client.get('/auth/perfil', query_string={'token': 'demo-anon'})

    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    assert data["widget_session_active"] is True
    assert data["session_kind"] == "widget"


def test_demo_slug_token_fallback_uses_rubro(monkeypatch, client):
    """Tokens like demo-ferreteria should resolve via rubro aliases even without registry."""

    rubro = Rubro.query.filter_by(clave="ferreteria").first()
    if not rubro:
        rubro = Rubro(nombre="Ferretería", clave="ferreteria", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="fallback-ferreteria-owner@test.com",
        name="Ferretería Demo Owner",
        rol="admin",
        token="ferreteria-demo-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    monkeypatch.setattr("utils.auth_helpers.demo_rubro_for_token", lambda *_: None)

    response = client.get('/auth/perfil', query_string={'token': 'demo-ferreteria'})

    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    assert data["widget_session_active"] is True
    assert data["session_kind"] == "widget"


def test_legacy_perfil_accepts_demo_token(client, monkeypatch):
    """Legacy /perfil route should also resolve demo tokens and expose legacy fields."""

    monkeypatch.setattr("utils.auth_helpers.demo_rubro_for_token", lambda *_: None)

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipal", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="legacy-demo-owner@test.com",
        name="Legacy Demo Owner",
        rol="admin",
        token="legacy-demo-owner-token",
        rubro_id=rubro.id,
        tipo_chat="municipio",
        telefono="123456",
        direccion="Av. Principal 123",
        ciudad="Junín",
        provincia="Buenos Aires",
        pais="Argentina",
        preguntas_usadas=5,
        plan="pro",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    response = client.get('/perfil', query_string={'token': 'demo-anon'})

    assert response.status_code == 200
    data = response.get_json()

    assert data["entity_token"] == owner.token
    assert data["widget_embed_token"] is None
    assert data["owner_token"] == owner.token
    assert data["widget_embed_token_kind"] == "plan_required"
    assert data["widget_session_active"] is True
    assert data["session_kind"] == "widget"
    assert data["session_token"] and data["session_token"].count('.') == 2
    assert data["auth_token"] == data["session_token"]
    assert data["token"] == data["session_token"]
    assert data["telefono"] == "123456"
    assert data["direccion"] == "Av. Principal 123"
    assert data["ciudad"] == "Junín"
    assert data["provincia"] == "Buenos Aires"
    assert data["pais"] == "Argentina"
    assert data["preguntas_usadas"] == 5
    assert data["limite_preguntas"] == 250  # plan pro via limite_para_usuario
    assert data["catalogo_label"] == "Cargar Catálogo de Trámites"

def test_login_jwt_wins_over_entity_token_header(client):
    """Panel requests must keep using the login JWT even if they also send the entity token."""

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipalidad", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    admin = User(
        email="admin-entity-header@test.com",
        name="Panel Admin",
        rol="admin",
        token="admin-entity-token",
        rubro_id=rubro.id,
        tipo_chat="municipio",
    )
    admin.set_password("pw")
    db.session.add(admin)
    db.session.commit()

    jwt_payload = {
        "user_id": admin.id,
        "exp": datetime.utcnow() + timedelta(days=1),
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    response = client.get(
        "/auth/me",
        headers={
            "Authorization": f"Bearer {jwt_token}",
            "X-Entity-Token": admin.token,
        },
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["auth_token"] == jwt_token
    assert data["entity_token"] == admin.token

    widget_cookie_name = client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    assert all(
        not cookie_header.startswith(f"{widget_cookie_name}=")
        for cookie_header in response.headers.getlist("Set-Cookie")
    )


def test_widget_cookie_is_scoped_to_owner_token(client):
    """A widget cookie from owner A must not be reused when owner B supplies its token."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner_a = User(
        email="widget-cookie-a@test.com",
        name="Owner A",
        token="owner-a-static-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner_a.set_password("pw")

    owner_b = User(
        email="widget-cookie-b@test.com",
        name="Owner B",
        token="owner-b-static-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner_b.set_password("pw")

    db.session.add_all([owner_a, owner_b])
    db.session.commit()

    assert owner_a.id != owner_b.id

    db_owner_a = User.query.filter_by(token=owner_a.token).first()
    db_owner_b = User.query.filter_by(token=owner_b.token).first()
    assert db_owner_a is not None and db_owner_a.id == owner_a.id
    assert db_owner_b is not None and db_owner_b.id == owner_b.id

    from utils.auth_helpers import _lookup_owner_for_static_token

    assert _lookup_owner_for_static_token(owner_b.token).id == owner_b.id

    first = client.get('/auth/perfil', query_string={'token': owner_a.token})
    assert first.status_code == 200
    first_data = first.get_json()
    token_a = first_data["auth_token"]
    assert token_a and token_a.count('.') == 2

    second = client.get('/auth/perfil', query_string={'token': owner_b.token})
    assert second.status_code == 200
    second_data = second.get_json()
    assert second_data["entity_token"] == owner_b.token
    token_b = second_data["auth_token"]
    assert token_b and token_b.count('.') == 2
    assert token_b != token_a

    widget_cookie_name = client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    cookie_headers = second.headers.getlist("Set-Cookie")
    assert any(
        cookie_header.startswith(f"{widget_cookie_name}=") and token_b in cookie_header
        for cookie_header in cookie_headers
    )


def test_expired_widget_cookie_does_not_block_renewal(client):
    """If the widget cookie expired, the static token should trigger a fresh session."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="widget-cookie-expired@test.com",
        name="Widget Owner Expired",
        token="static-owner-expired",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    now = datetime.utcnow()
    expired_payload = {
        "user_id": owner.id,
        "rol": getattr(owner, "rol", None),
        "tipo_chat": owner.tipo_chat,
        "municipio_id": getattr(owner, "municipio_id", None),
        "pyme_id": getattr(owner, "pyme_id", None),
        "session_kind": "widget",
        "iat": int((now - timedelta(minutes=30)).timestamp()),
        "exp": int((now - timedelta(minutes=5)).timestamp()),
        "renew_until": int((now + timedelta(days=1)).timestamp()),
    }
    expired_token = jwt.encode(
        expired_payload,
        current_app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    if isinstance(expired_token, bytes):
        expired_token = expired_token.decode("utf-8")

    widget_cookie_name = client.application.config.get("WIDGET_TOKEN_COOKIE_NAME", "widget_token")
    client.set_cookie(key=widget_cookie_name, value=expired_token)

    response = client.get('/auth/perfil', query_string={'token': owner.token})
    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    renewed_token = data["auth_token"]
    assert renewed_token and renewed_token != expired_token
    assert renewed_token.count('.') == 2


def test_me_generates_entity_token_for_admin_when_missing(client):
    """Admins without a stored entity token should receive one automatically."""

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipalidad", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    admin = User(
        email="entity-mint-admin@test.com",
        name="Entity Mint Admin",
        rol="admin",
        rubro_id=rubro.id,
        tipo_chat="municipio",
    )
    admin.set_password("pw")
    admin.token = None
    db.session.add(admin)
    db.session.commit()

    jwt_payload = {
        "user_id": admin.id,
        "exp": datetime.utcnow() + timedelta(days=1),
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    headers = {"Authorization": f"Bearer {jwt_token}"}

    first_response = client.get("/auth/me", headers=headers)
    assert first_response.status_code == 200
    first_data = first_response.get_json()
    assert first_data["entity_token"]
    stored = User.query.get(admin.id)
    assert stored.token == first_data["entity_token"]

    second_response = client.get("/auth/me", headers=headers)
    assert second_response.status_code == 200
    second_data = second_response.get_json()
    assert second_data["entity_token"] == first_data["entity_token"]


def test_widget_jwt_stays_anonymous_in_anon_decorator(app, client):
    """Widget session JWTs must not authenticate as the owner when using anon endpoints."""

    rubro = Rubro.query.filter_by(clave="pyme").first()
    if not rubro:
        rubro = Rubro(nombre="pyme", clave="pyme", es_publico=False)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="widget-session-owner@test.com",
        name="Widget Session Owner",
        token="widget-session-static-token",
        rubro_id=rubro.id,
        tipo_chat="pyme",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    from flask import jsonify, g

    @anon_o_token_requerido
    def widget_view(current_user=None, owner_user=None, anon_id=None):
        return jsonify(
            {
                "current_user_id": getattr(current_user, "id", None) if current_user else None,
                "owner_user_id": getattr(owner_user, "id", None) if owner_user else None,
                "widget_session": getattr(g, "widget_session", False),
                "anon_id": anon_id,
            }
        )

    perfil = client.get("/auth/perfil", query_string={"token": owner.token})
    assert perfil.status_code == 200
    widget_token = perfil.get_json()["auth_token"]
    assert widget_token and widget_token.count(".") == 2

    with app.test_request_context(
        "/test/widget-view",
        method="GET",
        headers={"Authorization": f"Bearer {widget_token}"},
    ):
        response = widget_view()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["current_user_id"] is None
    assert payload["owner_user_id"] == owner.id
    assert payload["widget_session"] is True
    assert payload["anon_id"]

def test_panel_jwt_behaves_like_widget_in_anon_decorator(app, client):
    """Panel JWTs should behave like anonymous widget viewers on anon endpoints."""

    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="municipio", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.commit()

    owner = User(
        email="panel-owner@test.com",
        name="Panel Owner",
        rubro_id=rubro.id,
        tipo_chat="municipio",
    )
    owner.set_password("pw")
    db.session.add(owner)
    db.session.commit()

    jwt_payload = {
        "user_id": owner.id,
        "exp": datetime.utcnow() + timedelta(days=1),
    }
    jwt_token = jwt.encode(jwt_payload, current_app.config["SECRET_KEY"], algorithm="HS256")
    if isinstance(jwt_token, bytes):
        jwt_token = jwt_token.decode("utf-8")

    from flask import jsonify, g

    @anon_o_token_requerido
    def widget_view(current_user=None, owner_user=None, anon_id=None):
        return jsonify(
            {
                "current_user_id": getattr(current_user, "id", None) if current_user else None,
                "owner_user_id": getattr(owner_user, "id", None) if owner_user else None,
                "widget_session": getattr(g, "widget_session", False),
                "anon_id": anon_id,
            }
        )

    with app.test_request_context(
        "/test/widget-view",
        method="GET",
        headers={"Authorization": f"Bearer {jwt_token}"},
    ):
        response = widget_view()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["current_user_id"] is None
    assert payload["owner_user_id"] == owner.id
    assert payload["widget_session"] is True
    assert payload["anon_id"]
