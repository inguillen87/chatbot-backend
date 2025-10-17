import pytest

from app import db
from models import User
from services.encuestas_service import (
    EncEncuesta,
    EncRespuesta,
    create_encuesta,
    publicar_encuesta,
    save_respuesta,
)
import services.encuestas_service as encuestas_service_module
from services.encuestas_anchor_service import build_snapshot
import config.feature_flags as feature_flags
import routes.encuestas_admin as encuestas_admin_routes
import routes.encuestas_analytics as encuestas_analytics_routes
import routes.encuestas_anchor as encuestas_anchor_routes


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


def _auth_headers(client, admin_user):
    login_resp = client.post(
        "/auth/login",
        json={"email": admin_user.email, "password": "demo1234"},
    )
    assert login_resp.status_code == 200
    token = login_resp.get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_encuestas_alias_exposes_rest_endpoints(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    # OPTIONS requests should succeed without authentication so the widget can run preflight checks.
    options_resp = client.options("/admin/encuestas")
    assert options_resp.status_code in {200, 204}
    assert "Access-Control-Allow-Origin" in options_resp.headers

    headers = _auth_headers(client, admin_user)

    # The legacy alias should reuse the admin handlers and automatically bootstrap the Junín sample survey.
    list_resp = client.get("/admin/encuestas", headers=headers)
    assert list_resp.status_code == 200
    listado = list_resp.get_json()
    assert isinstance(listado, dict)
    assert "encuestas" in listado
    assert "resumen" in listado
    assert "seed_demo" in listado
    seed_config = listado["seed_demo"]
    assert "profiles" in seed_config
    assert seed_config["profiles"], "Se esperaba catálogo de perfiles geo demo"
    assert seed_config["defaults"]["cantidad"] == 100
    encuestas = listado["encuestas"]
    assert isinstance(encuestas, list)
    initial_total = listado["resumen"].get("total", 0)
    assert all(encuesta["tenant_id"] == admin_user.municipio_id for encuesta in encuestas)
    for encuesta_payload in encuestas:
        assert "geo" in encuesta_payload
        assert encuesta_payload["geo"].get("bounds") is not None
        assert "seed_demo" in encuesta_payload
        assert encuesta_payload["seed_demo"]["endpoint"].endswith("seed-demo")
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

    publish_resp = client.post(f"/admin/encuestas/{created_id}/publicar", headers=headers)
    assert publish_resp.status_code == 200

    # Listing again should include both the bootstrap survey and the new one.
    refreshed = client.get("/admin/encuestas", headers=headers).get_json()
    assert refreshed["resumen"]["total"] == initial_total + 1
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


def test_admin_templates_endpoint_returns_catalog(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    headers = _auth_headers(client, admin_user)

    resp = client.get(
        "/admin/encuestas/templates?municipality=Junin&include_draft=1",
        headers=headers,
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert isinstance(payload, dict)
    templates = payload.get("templates")
    assert isinstance(templates, list)
    assert templates, "Se esperaba al menos una plantilla"

    primera = templates[0]
    assert "titulo" in primera
    assert "Junin" in primera["titulo"] or "Junín" in primera["titulo"]
    assert isinstance(primera.get("preguntas"), list)

    draft = primera.get("draft")
    assert isinstance(draft, dict)
    assert draft.get("municipality") == "Junin"
    assert draft.get("slug", "").endswith("-junin")
    assert isinstance(draft.get("preguntas"), list) and draft["preguntas"]
    assert any(p.get("tipo") in {"opcion_unica", "multiple", "abierta"} for p in draft["preguntas"])


def test_admin_encuestas_listado_respuestas(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_analytics_routes, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_anchor_routes, "FEATURE_ENCUESTAS", True)

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

    headers = _auth_headers(client, admin_user)

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


def test_update_encuesta_accepts_multiple_choice_edit(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    headers = _auth_headers(client, admin_user)

    create_payload = {
        "titulo": "Encuesta editable",
        "descripcion": "Versión base",
        "tipo": "opinion",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "abierta",
                "texto": "Comentario inicial",
                "obligatoria": False,
            }
        ],
    }

    create_resp = client.post("/admin/encuestas", json=create_payload, headers=headers)
    assert create_resp.status_code == 201
    encuesta_id = create_resp.get_json()["id"]

    update_payload = {
        "titulo": "Encuesta editable",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "multiple_choice",
                "texto": "¿Qué iniciativas priorizarías?",
                "obligatoria": True,
                "minSeleccion": "1",
                "maxSeleccion": "3",
                "opciones": [
                    {"texto": "Reducir residuos"},
                    "Programas de reciclaje",
                    {"label": "Educación ambiental", "valor": "educacion"},
                ],
            }
        ],
    }

    update_resp = client.put(
        f"/admin/encuestas/{encuesta_id}", json=update_payload, headers=headers
    )
    assert update_resp.status_code == 200
    updated = update_resp.get_json()

    assert updated["preguntas"] and updated["preguntas"][0]["tipo"] == "opcion_multiple"
    pregunta = updated["preguntas"][0]
    assert pregunta["min_selecciones"] == 1
    assert pregunta["max_selecciones"] == 3
    assert len(pregunta["opciones"]) == 3
    assert [opt["texto"] for opt in pregunta["opciones"]] == [
        "Reducir residuos",
        "Programas de reciclaje",
        "Educación ambiental",
    ]

    detail_resp = client.get(f"/admin/encuestas/{encuesta_id}", headers=headers)
    assert detail_resp.status_code == 200
    detalle = detail_resp.get_json()
    detalle_pregunta = detalle["preguntas"][0]
    assert detalle_pregunta["tipo"] == "opcion_multiple"
    assert detalle_pregunta["min_selecciones"] == 1
    assert detalle_pregunta["max_selecciones"] == 3
    assert len(detalle_pregunta["opciones"]) == 3


def test_admin_encuestas_legacy_analytics_and_snapshots(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_analytics_routes, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_anchor_routes, "FEATURE_ENCUESTAS", True)

    with client.application.app_context():
        payload = {
            "titulo": "Encuesta analítica",
            "descripcion": "Validamos alias legacy de analytics.",
            "tipo": "opinion",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Te gusta el panel?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Sí"},
                        {"orden": 2, "texto": "No"},
                    ],
                }
            ],
        }

        encuesta = create_encuesta(payload, admin_user)
        encuesta, link = publicar_encuesta(encuesta.id, admin_user)
        encuesta = db.session.get(EncEncuesta, encuesta.id)
        encuesta.inicio_at = None
        encuesta.fin_at = None
        db.session.commit()

        respuesta_payload = {
            "respuestas": [
                {
                    "pregunta_id": encuesta.preguntas[0].id,
                    "opcion_ids": [encuesta.preguntas[0].opciones[0].id],
                }
            ],
            "utm_source": "widget",
            "utm_campaign": "analytics-test",
            "canal": "web",
        }
        request_ctx = {"ip": "10.0.0.2", "user_agent": "pytest", "anon_id": "analytics", "canal": "web"}
        save_respuesta(link.slug_publico, respuesta_payload, request_ctx)

        build_snapshot(
            encuesta.id,
            "2020-01-01T00:00:00Z",
            "2030-01-01T00:00:00Z",
            admin_user,
        )

        encuesta_id = encuesta.id

    headers = _auth_headers(client, admin_user)

    resumen_resp = client.get(f"/admin/encuestas/{encuesta_id}/analytics/resumen", headers=headers)
    assert resumen_resp.status_code == 200
    resumen_data = resumen_resp.get_json()
    assert resumen_data["encuesta_id"] == encuesta_id
    assert resumen_data["total_respuestas"] >= 1

    series_resp = client.get(f"/admin/encuestas/{encuesta_id}/analytics/series", headers=headers)
    assert series_resp.status_code == 200
    series_data = series_resp.get_json()
    assert isinstance(series_data, list)
    assert series_data
    assert {"fecha", "total"}.issubset(series_data[0].keys())

    heatmap_resp = client.get(f"/admin/encuestas/{encuesta_id}/analytics/heatmap", headers=headers)
    assert heatmap_resp.status_code == 200
    heatmap_data = heatmap_resp.get_json()
    assert "points" in heatmap_data
    assert "cells" in heatmap_data
    assert "metadata" in heatmap_data
    assert heatmap_data["metadata"]["resolution"] >= 1

    snapshots_resp = client.get(f"/admin/encuestas/{encuesta_id}/snapshots", headers=headers)
    assert snapshots_resp.status_code == 200
    snapshots_data = snapshots_resp.get_json()
    assert snapshots_data["encuesta_id"] == encuesta_id
    assert snapshots_data["snapshots"]
    snapshot = snapshots_data["snapshots"][0]
    assert snapshot["total_respuestas"] >= 1


def test_admin_encuestas_permite_actualizar_publicada_sin_respuestas(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    with client.application.app_context():
        payload = {
            "titulo": "Encuesta de actualización",
            "descripcion": "Contenido inicial",
            "tipo": "opinion",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Usás el chatbot?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Sí"},
                        {"orden": 2, "texto": "No"},
                    ],
                }
            ],
        }

        encuesta = create_encuesta(payload, admin_user)
        publicar_encuesta(encuesta.id, admin_user)
        encuesta_id = encuesta.id

    headers = _auth_headers(client, admin_user)

    update_payload = {
        "descripcion": "Contenido actualizado",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Recomendarías el chatbot?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Claro que sí"},
                    {"orden": 2, "texto": "Aún no"},
                ],
            }
        ],
    }

    resp = client.put(f"/admin/encuestas/{encuesta_id}", json=update_payload, headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["descripcion"] == "Contenido actualizado"
    assert data["preguntas"][0]["texto"] == "¿Recomendarías el chatbot?"


def test_admin_encuestas_publicada_con_respuestas_bloquea_cambio_estructura(
    client, monkeypatch, admin_user
):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    with client.application.app_context():
        payload = {
            "titulo": "Encuesta con respuestas",
            "tipo": "opinion",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Te gusta el servicio?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Sí"},
                        {"orden": 2, "texto": "No"},
                    ],
                }
            ],
        }

        encuesta = create_encuesta(payload, admin_user)
        encuesta, link = publicar_encuesta(encuesta.id, admin_user)
        encuesta = db.session.get(EncEncuesta, encuesta.id)
        encuesta.inicio_at = None
        encuesta.fin_at = None
        db.session.commit()
        pregunta = encuesta.preguntas[0]
        respuesta_payload = {
            "respuestas": [
                {"pregunta_id": pregunta.id, "opcion_ids": [pregunta.opciones[0].id]},
            ],
            "canal": "web",
        }
        request_ctx = {"ip": "127.0.0.1", "user_agent": "pytest", "anon_id": "test", "canal": "web"}
        save_respuesta(link.slug_publico, respuesta_payload, request_ctx)
        encuesta_id = encuesta.id

    headers = _auth_headers(client, admin_user)

    update_payload = {
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Quieres actualizar la respuesta?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Sí"},
                    {"orden": 2, "texto": "No"},
                ],
            }
        ]
    }

    resp = client.put(f"/admin/encuestas/{encuesta_id}", json=update_payload, headers=headers)
    assert resp.status_code == 409
    data = resp.get_json()
    assert "No se puede modificar la estructura" in data["error"]


def test_admin_encuestas_seed_demo_endpoint(client, monkeypatch, admin_user):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(encuestas_admin_routes, "FEATURE_ENCUESTAS", True)

    headers = _auth_headers(client, admin_user)

    payload = {
        "titulo": "Encuesta demo sin respuestas",
        "descripcion": "Probaremos la carga automática de respuestas.",
        "tipo": "opinion",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Participaste de actividades municipales?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Sí"},
                    {"orden": 2, "texto": "No"},
                ],
            }
        ],
    }

    create_resp = client.post("/admin/encuestas", json=payload, headers=headers)
    assert create_resp.status_code == 201
    encuesta_id = create_resp.get_json()["id"]

    seed_resp = client.post(
        f"/admin/encuestas/{encuesta_id}/seed-demo",
        json={"cantidad": 8},
        headers=headers,
    )
    assert seed_resp.status_code == 200
    seed_payload = seed_resp.get_json()
    assert seed_payload["creadas"] > 0
    assert "seed" in seed_payload

    with client.application.app_context():
        total = EncRespuesta.query.filter_by(encuesta_id=encuesta_id).count()
        assert total == seed_payload["creadas"]
        geo_count = EncRespuesta.query.filter_by(encuesta_id=encuesta_id).filter(
            EncRespuesta.lat.isnot(None), EncRespuesta.lng.isnot(None)
        ).count()
        assert geo_count > 0
