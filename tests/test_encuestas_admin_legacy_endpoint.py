import pytest

from app import db
from models import User
from services.encuestas_service import EncEncuesta
import config.feature_flags as feature_flags
import routes.encuestas_admin as encuestas_admin_routes


@pytest.fixture
def admin_user():
    admin = User(
        email="junin-admin@example.com",
        name="Junín Admin",
        rol="admin",
        municipio_id=4,
        tipo_chat="municipio",
    )
    admin.set_password("demo1234")
    db.session.add(admin)
    db.session.commit()
    return admin


def test_admin_encuestas_alias_exposes_rest_endpoints(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    # OPTIONS requests should succeed without authentication so the widget can run preflight checks.
    options_resp = client.options("/admin/encuestas")
    assert options_resp.status_code in {200, 204}
    assert "Access-Control-Allow-Origin" in options_resp.headers

    login_resp = client.post(
        "/auth/login",
        json={"email": admin_user.email, "password": "demo1234"},
    )
    assert login_resp.status_code == 200
    token = login_resp.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    # The legacy alias should reuse the admin handlers and automatically bootstrap the Junín sample survey.
    list_resp = client.get("/admin/encuestas", headers=headers)
    assert list_resp.status_code == 200
    encuestas = list_resp.get_json()
    assert isinstance(encuestas, list)
    assert any("Junín" in encuesta["titulo"] for encuesta in encuestas)

    payload = {
        "titulo": "Encuesta piloto de servicios", 
        "descripcion": "Validamos la creación desde el alias legado.",
        "tipo": "opinion",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Cómo calificás la limpieza de tu barrio?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Excelente"},
                    {"orden": 2, "texto": "Buena"},
                    {"orden": 3, "texto": "Necesita mejoras"},
                ],
            }
        ],
    }

    create_resp = client.post("/admin/encuestas", json=payload, headers=headers)
    assert create_resp.status_code == 201
    created_id = create_resp.get_json()["id"]
    assert db.session.get(EncEncuesta, created_id) is not None

    # Listing again should include both the bootstrap survey and the new one.
    refreshed = client.get("/admin/encuestas", headers=headers).get_json()
    assert len(refreshed) >= 2

    public_resp = client.get("/public/encuestas")
    assert public_resp.status_code == 200
    public_data = public_resp.get_json()
    assert isinstance(public_data, list)
    assert any("Junín" in encuesta.get("titulo", "") for encuesta in public_data)
