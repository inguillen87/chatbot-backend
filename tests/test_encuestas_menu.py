import pytest
from types import SimpleNamespace

import config.feature_flags as feature_flags
import services.municipio_responder as municipio_responder
from database import db
from services.encuestas_service import create_encuesta, publicar_encuesta


class DummyUser:
    def __init__(self, tenant_id: int = 1):
        self.id = None
        self.municipio_id = tenant_id
        self.empresa_id = None
        self.pyme_id = None


def _create_active_encuesta(tenant_id: int = 1):
    user = DummyUser(tenant_id)
    payload = {
        "titulo": "Encuesta Demo",
        "descripcion": "Participación ciudadana demo",
        "tipo": "opinion",
        "preguntas": [
            {
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Estás de acuerdo?",
                "obligatoria": True,
                "opciones": [
                    {"orden": 1, "texto": "Sí"},
                    {"orden": 2, "texto": "No"},
                ],
            }
        ],
    }
    encuesta = create_encuesta(payload, user)
    encuesta, link = publicar_encuesta(encuesta.id, user)
    db.session.refresh(encuesta)
    return encuesta, link.slug_publico


@pytest.fixture(autouse=True)
def reset_feature_flag(monkeypatch):
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", False)
    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS_TENANTS", set())
    monkeypatch.setattr(
        feature_flags,
        "is_feature_encuestas_enabled_for_tenant",
        lambda tenant_id: False,
    )
    monkeypatch.setattr(
        municipio_responder,
        "is_feature_encuestas_enabled_for_tenant",
        lambda tenant_id: False,
    )
    yield


def _base_context(tenant_id: int, extra_config: dict | None = None):
    config = extra_config or {}
    return {
        "municipio_id": tenant_id,
        "user_obj": SimpleNamespace(municipio_id=tenant_id),
        "municipio_config_actual": config,
    }


def test_encuestas_menu_auto_enabled_by_active_surveys(client):
    with client.application.app_context():
        encuesta, slug = _create_active_encuesta(tenant_id=7)
        context = _base_context(tenant_id=encuesta.tenant_id or 7)
        menu = municipio_responder._get_encuestas_menu(context)

    assert "Participación Ciudadana" in menu["message_body"]
    assert slug in menu["message_body"]
    assert any(option.get("type") == "url" for option in menu["options_list"])


def test_encuestas_menu_respects_explicit_disable(client):
    with client.application.app_context():
        encuesta, _ = _create_active_encuesta(tenant_id=3)
        context = _base_context(
            tenant_id=encuesta.tenant_id or 3,
            extra_config={"encuestas_enabled": False},
        )
        menu = municipio_responder._get_encuestas_menu(context)

    assert "todavía no están habilitadas" in menu["message_body"].lower()
    assert menu["options_list"][-1]["action_id"] == "cancelar"


def test_encuestas_menu_shows_empty_state_when_enabled(client):
    with client.application.app_context():
        context = _base_context(
            tenant_id=1,
            extra_config={"encuestas": {"enabled": True}},
        )
        menu = municipio_responder._get_encuestas_menu(context)

    assert "por el momento no hay encuestas activas" in menu["message_body"].lower()
    assert menu["fuente"] == "submenu_encuestas_v1"
