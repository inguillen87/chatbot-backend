from models import User, Rubro, db
import jwt
from datetime import datetime, timedelta
from flask import current_app

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
