import json
import jwt
from datetime import datetime, timedelta
from app import db


def _auth_headers(app, user_id):
    token = jwt.encode(
        {"user_id": user_id, "exp": datetime.utcnow() + timedelta(days=1)},
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def test_accessibility_preferences_crud(client, app, init_database, viewer_user):
    headers = _auth_headers(app, viewer_user.id)

    resp = client.get(f"/api/accessibility/{viewer_user.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.get_json() == {"dyslexia": False, "simplified": True}

    payload = {"dyslexia": True, "simplified": False}
    resp = client.put(
        f"/api/accessibility/{viewer_user.id}",
        headers=headers,
        data=json.dumps(payload),
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["dyslexia"] is True
    assert data["simplified"] is False

    db.session.refresh(viewer_user)
    assert viewer_user.accesibilidad["dyslexia"] is True
    assert viewer_user.accesibilidad["simplified"] is False

    resp = client.get(f"/api/accessibility/{viewer_user.id}", headers=headers)
    assert resp.status_code == 200
    data_get = resp.get_json()
    assert data_get["dyslexia"] is True
    assert data_get["simplified"] is False


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
