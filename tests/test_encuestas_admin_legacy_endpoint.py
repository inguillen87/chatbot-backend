import pytest

from app import db
from models import User
from services.encuestas_service import EncEncuesta, create_encuesta, publicar_encuesta, save_respuesta
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
    listado = list_resp.get_json()
    assert isinstance(listado, dict)
    assert "encuestas" in listado
    assert "resumen" in listado
    encuestas = listado["encuestas"]
    assert isinstance(encuestas, list)
    assert any("Junín" in encuesta["titulo"] for encuesta in encuestas)
    assert all(encuesta["tenant_id"] == admin_user.municipio_id for encuesta in encuestas)
    resumen = listado["resumen"]
    assert resumen["total"] == len(encuestas)
    assert resumen["activas"] <= resumen["total"]

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
    assert refreshed["resumen"]["total"] >= 2
    assert all(
        encuesta["tenant_id"] == admin_user.municipio_id for encuesta in refreshed["encuestas"]
    )
    assert refreshed["resumen"]["con_respuestas"] >= 0

    legacy_resp = client.get("/admin/encuestas?legacy=1", headers=headers)
    assert legacy_resp.status_code == 200
    legacy_payload = legacy_resp.get_json()
    assert isinstance(legacy_payload, list)
    assert len(legacy_payload) == refreshed["resumen"]["total"]

    public_resp = client.get("/public/encuestas")
    assert public_resp.status_code == 200
    public_data = public_resp.get_json()
    assert isinstance(public_data, list)
    assert any("Junín" in encuesta.get("titulo", "") for encuesta in public_data)


def test_admin_encuestas_listado_respuestas(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    with client.application.app_context():
        payload = {
            "titulo": "Encuesta de satisfacción plazas",
            "descripcion": "Validamos la visualización de respuestas.",
            "tipo": "opinion",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Visitás la plaza cada semana?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Sí"},
                        {"orden": 2, "texto": "No"},
                    ],
                },
                {
                    "orden": 2,
                    "tipo": "abierta",
                    "texto": "Comentarios",
                    "obligatoria": False,
                },
            ],
        }

        encuesta = create_encuesta(payload, admin_user)
        encuesta, link = publicar_encuesta(encuesta.id, admin_user)
        encuesta = db.session.get(EncEncuesta, encuesta.id)
        encuesta.inicio_at = None
        encuesta.fin_at = None
        db.session.commit()

        pregunta_opcion = next(p for p in encuesta.preguntas if p.tipo == "opcion_unica")
        pregunta_abierta = next(p for p in encuesta.preguntas if p.tipo == "abierta")
        respuesta_payload = {
            "respuestas": [
                {"pregunta_id": pregunta_opcion.id, "opcion_ids": [pregunta_opcion.opciones[0].id]},
                {"pregunta_id": pregunta_abierta.id, "texto_libre": "Muy linda iluminación"},
            ],
            "utm_source": "widget",
            "utm_campaign": "lanzamiento",
            "canal": "web",
        }
        request_ctx = {"ip": "10.0.0.1", "user_agent": "pytest", "anon_id": "test-admin", "canal": "web"}
        save_respuesta(link.slug_publico, respuesta_payload, request_ctx)
        encuesta_id = encuesta.id

    login_resp = client.post(
        "/auth/login",
        json={"email": admin_user.email, "password": "demo1234"},
    )
    assert login_resp.status_code == 200
    token = login_resp.get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    listado_resp = client.get(f"/admin/encuestas/{encuesta_id}/respuestas", headers=headers)
    assert listado_resp.status_code == 200
    data = listado_resp.get_json()
    assert data["total"] >= 1
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert data["respuestas"]
    primera = data["respuestas"][0]
    assert primera["utm_source"] == "widget"
    assert any(det.get("texto_libre") for det in primera["detalles"])
