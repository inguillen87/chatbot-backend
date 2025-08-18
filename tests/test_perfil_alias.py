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
