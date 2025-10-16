import pytest
from datetime import timedelta
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
    monkeypatch.setattr(municipio_responder, "FEATURE_ENCUESTAS", False)
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
        app = client.application
        previous_default_image = app.config.get(
            "PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"
        )
        fallback_image = (
            "https://cdn.chatboc.ar/static/encuestas/participacion_ciudadana.png"
        )
        app.config["PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"] = fallback_image
        try:
            menu = municipio_responder._get_encuestas_menu(context)
        finally:
            if previous_default_image is None:
                app.config.pop("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL", None)
            else:
                app.config["PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"] = (
                    previous_default_image
                )

    body = menu["message_body"]
    assert "Participación Ciudadana" in body
    assert "Últimas encuestas disponibles" in body
    assert slug in body
    assert "Abrir la encuesta en la web:" in body
    assert "Compartir con un mensaje listo para WhatsApp" in body
    assert "Descargar el código QR" not in body
    assert "Usar el asistente virtual en la web" not in body
    assert any(option.get("type") == "url" for option in menu["options_list"])
    button_urls = [
        option.get("url", "")
        for option in menu["options_list"]
        if option.get("type") == "url"
    ]
    assert any(url.endswith(f"/e/{slug}") for url in button_urls)
    assert any(url.startswith("https://wa.me/") for url in button_urls)
    assert not any(url.endswith(f"/e/{slug}?canal=widget_chat") for url in button_urls)
    assert not any(url.endswith(f"/api/public/encuestas/{slug}/qr") for url in button_urls)
    media_urls = menu.get("media_urls")
    assert isinstance(media_urls, list) and len(media_urls) == 1
    assert media_urls[0] == fallback_image
    assert menu.get("image_url") == fallback_image
    assert menu.get("_base_url") == client.application.config.get(
        "PUBLIC_ENCUESTAS_API_BASE_URL"
    )
    surveys_meta = menu.get("surveys")
    assert isinstance(surveys_meta, list) and surveys_meta
    first_meta = surveys_meta[0]
    assert first_meta["slug"] == slug
    assert first_meta["share_url"].endswith(f"/e/{slug}")
    assert first_meta["whatsapp_share_url"].startswith("https://wa.me/")
    assert first_meta["qr_url"].endswith(f"/api/public/encuestas/{slug}/qr")


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
    assert "surveys" not in menu


def test_encuestas_menu_shows_empty_state_when_enabled(client):
    with client.application.app_context():
        context = _base_context(
            tenant_id=1,
            extra_config={"encuestas": {"enabled": True}},
        )
        menu = municipio_responder._get_encuestas_menu(context)

    assert "por el momento no hay encuestas activas" in menu["message_body"].lower()
    assert menu["fuente"] == "submenu_encuestas_v1"
    assert "surveys" not in menu


def test_encuestas_menu_prefers_domain_map_base_url(client):
    with client.application.app_context():
        encuesta, slug = _create_active_encuesta(tenant_id=11)
        app = client.application
        app.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None
        app.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = None
        app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {
            "chatbot-backend-2e14.onrender.com": 999,
            "www.chatboc.ar": encuesta.tenant_id,
            "chatboc.ar": encuesta.tenant_id,
        }
        app.config["IS_HTTPS"] = True
        context = _base_context(tenant_id=encuesta.tenant_id or 11)
        menu = municipio_responder._get_encuestas_menu(context)

    expected_prefix = "https://www.chatboc.ar/e/"
    expected_display_prefix = (
        expected_prefix.replace("https://", "").replace("http://", "").replace("www.", "")
    )
    assert f"Web: {expected_display_prefix}{slug}" in menu["message_body"]
    button_urls = [
        option.get("url", "")
        for option in menu["options_list"]
        if option.get("type") == "url"
    ]
    assert any(url.startswith(expected_prefix) for url in button_urls)
    assert any(url.startswith("https://wa.me/") for url in button_urls)


def test_encuestas_menu_includes_configured_image(client):
    with client.application.app_context():
        encuesta, _ = _create_active_encuesta(tenant_id=9)
        context = _base_context(
            tenant_id=encuesta.tenant_id or 9,
            extra_config={"encuestas": {"menu_image_url": "https://cdn.example.com/encuestas/banner.png"}},
        )
        menu = municipio_responder._get_encuestas_menu(context)

    assert menu.get("image_url") == "https://cdn.example.com/encuestas/banner.png"
    media_urls = menu.get("media_urls")
    assert media_urls and media_urls[0] == "https://cdn.example.com/encuestas/banner.png"


def test_encuestas_menu_defaults_to_backend_banner(client, monkeypatch):
    with client.application.app_context():
        encuesta, slug = _create_active_encuesta(tenant_id=13)
        context = _base_context(tenant_id=encuesta.tenant_id or 13)
        app = client.application
        app.config.pop("PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL", None)
        app.config["PUBLIC_ENCUESTAS_API_BASE_URL"] = "https://api.chatboc.ar"
        monkeypatch.setattr(
            municipio_responder.AppConfig,
            "PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL",
            None,
            raising=False,
        )
        menu = municipio_responder._get_encuestas_menu(context)

    expected_banner = "https://api.chatboc.ar/static/encuestas/participacion_ciudadana.png"
    assert menu.get("image_url") == expected_banner
    media_urls = menu.get("media_urls")
    assert media_urls and media_urls[0] == expected_banner
    assert menu.get("_base_url") == "https://api.chatboc.ar"
    assert slug in menu["message_body"]


def test_encuestas_menu_orders_newest_first(client):
    with client.application.app_context():
        encuesta_old, slug_old = _create_active_encuesta(tenant_id=21)
        encuesta_new, slug_new = _create_active_encuesta(tenant_id=21)

        encuesta_old.created_at = (encuesta_old.created_at or encuesta_new.created_at) - timedelta(days=7)
        db.session.commit()

        context = _base_context(tenant_id=encuesta_new.tenant_id or 21)
        menu = municipio_responder._get_encuestas_menu(context)

    body = menu["message_body"]
    assert body.index(slug_new) < body.index(slug_old)
