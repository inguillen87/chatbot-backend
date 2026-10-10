import json
import jwt
from datetime import datetime, timedelta, timezone
from app import db


def _auth_headers(app, user_id):
    token = jwt.encode(
        {"user_id": user_id, "exp": datetime.now(timezone.utc) + timedelta(days=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_accessibility_preferences_crud(client, app, init_database, viewer_user):
    headers = _auth_headers(app, viewer_user.id)

    resp = client.get(f"/api/accessibility/{viewer_user.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json() == {
        "dyslexia": False,
        "simplified": True,
        "highContrast": False,
        "largeControls": False,
        "captions": False,
        "reducedMotion": False,
    }

    payload = {
        "dyslexia": True,
        "simplified": False,
        "highContrast": True,
        "largeControls": True,
        "captions": True,
        "reducedMotion": True,
    }
    resp = client.put(
        f"/api/accessibility/{viewer_user.id}",
        headers=headers,
        data=json.dumps(payload),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["dyslexia"] is True
    assert data["simplified"] is False
    assert data["highContrast"] is True
    assert data["largeControls"] is True
    assert data["captions"] is True
    assert data["reducedMotion"] is True

    db.session.refresh(viewer_user)
    assert viewer_user.accesibilidad["dyslexia"] is True
    assert viewer_user.accesibilidad["simplified"] is False

    resp = client.get(f"/api/accessibility/{viewer_user.id}", headers=headers)
    assert resp.status_code == 200
    data_get = resp.get_json()
    assert data_get["dyslexia"] is True
    assert data_get["simplified"] is False


def test_accessibility_preferences_me_exposes_portable_contract(
    client, app, init_database, viewer_user
):
    headers = _auth_headers(app, viewer_user.id)

    initial = client.get("/api/accessibility/me", headers=headers)
    assert initial.status_code == 200
    assert initial.get_json() == {
        "contract_version": "user.accessibility_preferences.v1",
        "user_id": viewer_user.id,
        "initialized": False,
        "preferences": {
            "dyslexia": False,
            "simplified": True,
            "highContrast": False,
            "largeControls": False,
            "captions": False,
            "reducedMotion": False,
        },
    }

    updated = client.put(
        "/api/accessibility/me",
        headers=headers,
        data=json.dumps({"dyslexia": True, "reduced_motion": True}),
    )
    assert updated.status_code == 200
    body = updated.get_json()
    assert body["initialized"] is True
    assert body["preferences"]["dyslexia"] is True
    assert body["preferences"]["reducedMotion"] is True


def test_accessibility_preferences_rejects_ambiguous_or_unknown_values(
    client, app, init_database, viewer_user
):
    headers = _auth_headers(app, viewer_user.id)

    response = client.put(
        "/api/accessibility/me",
        headers=headers,
        data=json.dumps({"highContrast": "false", "invented": True}),
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["reason_code"] == "invalid_accessibility_preferences"
    assert body["details"]["boolean_fields_required"] == ["highContrast"]
    assert body["details"]["unknown_fields"] == ["invented"]

    db.session.refresh(viewer_user)
    assert not viewer_user.accesibilidad


def test_accessibility_preferences_canonicalize_aliases_and_preserve_metadata(
    client, app, init_database, viewer_user
):
    viewer_user.accesibilidad = {
        "dislexia": False,
        "dyslexia": True,
        "employee_scope": {"categorias": ["luminarias"]},
    }
    db.session.commit()
    headers = _auth_headers(app, viewer_user.id)

    initial = client.get("/api/accessibility/me", headers=headers)
    assert initial.status_code == 200
    assert initial.get_json()["preferences"]["dyslexia"] is True

    updated = client.put(
        "/api/accessibility/me",
        headers=headers,
        data=json.dumps({"dislexia": False}),
    )
    assert updated.status_code == 200
    assert updated.get_json()["preferences"]["dyslexia"] is False

    db.session.refresh(viewer_user)
    assert viewer_user.accesibilidad["dyslexia"] is False
    assert "dislexia" not in viewer_user.accesibilidad
    assert viewer_user.accesibilidad["employee_scope"] == {
        "categorias": ["luminarias"]
    }


def test_accessibility_preferences_cannot_read_another_account(
    client, app, init_database, viewer_user, owner_user
):
    headers = _auth_headers(app, viewer_user.id)
    response = client.get(f"/api/accessibility/{owner_user.id}", headers=headers)
    assert response.status_code == 403


def test_accessibility_preflight_preserves_credentials(
    client, app, init_database, viewer_user
):
    resp = client.options(
        f"/api/accessibility/{viewer_user.id}", headers={"Origin": "https://www.chatboc.ar"}
    )
    assert resp.status_code == 204
    assert resp.headers.get("Access-Control-Allow-Credentials") == "true"
    allowed_headers = resp.headers.get("Access-Control-Allow-Headers", "")
    assert "Authorization" in allowed_headers
    assert "X-Chat-Session-Id" in allowed_headers
