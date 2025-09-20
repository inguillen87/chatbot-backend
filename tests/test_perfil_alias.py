from models import User, Rubro, db
import jwt
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
    db.session.commit()

    # Primer llamado con el token estático: debe emitir un JWT de sesión de widget.
    response = client.get('/auth/perfil', query_string={'token': owner.token})
    assert response.status_code == 200
    data = response.get_json()
    assert data["entity_token"] == owner.token
    auth_token = data["auth_token"]
    assert auth_token and auth_token != owner.token
    assert auth_token.count('.') == 2

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
