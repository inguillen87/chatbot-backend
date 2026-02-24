import json

from app import db
from models import Rubro, User


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
    assert response.headers.get("X-Entity-Token") == "static-entity-token"
