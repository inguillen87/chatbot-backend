import json

from app import db
from models import Rubro, TenantProfile, User


def test_login_returns_persistent_entity_token(client):
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipio", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.flush()

    user = User(
        name="Mauricio",
        email="mauricio@test.com",
        rol="admin",
        rubro_id=rubro.id,
        token="static-entity-token",
    )
    user.set_password("123456")
    db.session.add(user)
    db.session.flush()

    tenant = TenantProfile(
        slug="mauricio-full-login",
        nombre="Mauricio Full Login",
        tipo="municipio",
        plan="full",
        municipio_id=user.id,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    user.tenant_slug = tenant.slug
    db.session.add(user)
    db.session.commit()

    response = client.post(
        "/auth/login",
        data=json.dumps({"email": user.email, "password": "123456"}),
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["entity_token"] == "static-entity-token"
    assert payload["widget_embed_token"] == "static-entity-token"
    assert isinstance((payload.get("ui") or {}).get("panels"), list)
    assert response.headers.get("X-Entity-Token") == "static-entity-token"


def test_login_and_session_bootstrap_expose_shell_first_contract(client):
    rubro = Rubro.query.filter_by(clave="municipio").first()
    if not rubro:
        rubro = Rubro(nombre="Municipio", clave="municipio", es_publico=True)
        db.session.add(rubro)
        db.session.flush()

    user = User(
        name="Bootstrap",
        email="bootstrap@test.com",
        rol="admin",
        rubro_id=rubro.id,
        token="bootstrap-entity-token",
    )
    user.set_password("123456")
    db.session.add(user)
    db.session.commit()

    login_resp = client.post(
        "/auth/login",
        data=json.dumps({"email": user.email, "password": "123456"}),
        content_type="application/json",
        headers={"X-Request-Id": "req-login-bootstrap"},
    )

    assert login_resp.status_code == 200
    login_payload = login_resp.get_json()
    assert (login_payload.get("bootstrap") or {}).get("endpoint") == "/auth/session/bootstrap"
    assert (login_payload.get("timing") or {}).get("db_lookup_ms") is not None
    assert (login_payload.get("timing") or {}).get("password_verify_ms") is not None
    assert (login_payload.get("timing") or {}).get("tenant_resolve_ms") is not None
    assert (login_payload.get("timing") or {}).get("token_sign_ms") is not None
    assert (login_payload.get("timing") or {}).get("total_ms") is not None
    assert login_resp.headers.get("X-Request-Id") == "req-login-bootstrap"
    assert login_resp.headers.get("Server-Timing")

    token = login_payload["token"]
    bootstrap_resp = client.get(
        "/auth/session/bootstrap",
        headers={"Authorization": f"Bearer {token}", "X-Request-Id": "req-bootstrap"},
    )

    assert bootstrap_resp.status_code == 200
    bootstrap_payload = bootstrap_resp.get_json()
    assert bootstrap_payload.get("request_id") == "req-bootstrap"
    assert (bootstrap_payload.get("bootstrap") or {}).get("timing_ms") is not None
    assert (bootstrap_payload.get("bootstrap") or {}).get("analytics", {}).get("seed_demo_endpoint_template", "").endswith("/seed-demo/bulk")
    assert bootstrap_resp.headers.get("X-Request-Id") == "req-bootstrap"
    assert bootstrap_resp.headers.get("Server-Timing")
