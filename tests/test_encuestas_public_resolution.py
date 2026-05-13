from database import db
from datetime import datetime, timedelta
import json

import pytest
from werkzeug.datastructures import MultiDict

from models import EncEncuesta, EncLink, User
from routes import encuestas_public
from services.encuestas_service import EncuestaError, get_public_encuesta, list_public_encuestas_for_tenant


class _DummyRespuesta:
    def __init__(self, respuesta_id):
        self.id = respuesta_id


def test_resolve_tenant_from_domain_map(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {"chatboc.ar": 7}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = None

    with app.test_request_context(
        "/public/encuestas",
        headers={"Host": "chatboc.ar"},
    ):
        assert encuestas_public._resolve_tenant_from_request() == 7


def test_resolve_tenant_from_forwarded_host(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {"chatboc.ar": 11}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = None

    with app.test_request_context(
        "/public/encuestas",
        headers={"X-Forwarded-Host": "api.chatboc.ar, www.chatboc.ar:443"},
    ):
        assert encuestas_public._resolve_tenant_from_request() == 11


def test_resolve_tenant_uses_default(app):
    app.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {}
    app.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = 5

    with app.test_request_context("/public/encuestas"):
        assert encuestas_public._resolve_tenant_from_request() == 5


def test_public_urls_use_canonical_base(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = "https://www.chatboc.ar"
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = "https://www.chatboc.ar"

    def fake_list(_tenant_id, limit):
        assert limit == 5
        return [({"id": 1}, "slug-demo")]

    monkeypatch.setattr(
        "routes.encuestas_public.list_public_encuestas_for_tenant",
        fake_list,
    )
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get("/public/encuestas")
    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["url_publica"] == "https://www.chatboc.ar/e/slug-demo"


def test_public_urls_honor_custom_target_base(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = "https://www.chatboc.ar"
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = "https://participa.junin.ar"

    def fake_list(_tenant_id, limit):
        assert limit == 5
        return [({"id": 1}, "slug-demo")]

    monkeypatch.setattr(
        "routes.encuestas_public.list_public_encuestas_for_tenant",
        fake_list,
    )
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get("/public/encuestas")
    assert response.status_code == 200
    data = response.get_json()
    assert data[0]["url_publica"] == "https://participa.junin.ar/e/slug-demo"


def test_respuestas_alias_reuses_handler(client, monkeypatch):
    saved_calls = {}

    def fake_save(slug, payload, ctx, **kwargs):
        saved_calls["slug"] = slug
        saved_calls["payload"] = payload
        saved_calls["ctx"] = ctx
        saved_calls["preferred_tenant_id"] = kwargs.get("preferred_tenant_id")
        return _DummyRespuesta(123)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    response = client.post(
        "/public/encuestas/demo-encuesta/respuestas",
        json={"respuesta": "ok"},
        headers={"X-Forwarded-For": "1.1.1.1"},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["ok"] is True
    assert body["respuesta_id"] == 123
    assert body["request_id"]
    assert saved_calls["slug"] == "demo-encuesta"
    assert saved_calls["payload"] == {"respuesta": "ok"}
    assert saved_calls["ctx"]["ip"] == "1.1.1.1"
    assert saved_calls["preferred_tenant_id"] == client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"]


def test_responder_accepts_form_payload(client, monkeypatch):
    captured: dict = {}

    def fake_save(slug, payload, ctx, **kwargs):
        captured["slug"] = slug
        captured["payload"] = payload
        captured["ctx"] = ctx
        captured["preferred_tenant_id"] = kwargs.get("preferred_tenant_id")
        return _DummyRespuesta(456)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    respuestas = [{"pregunta_id": 10, "opcion_ids": [2]}]
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data={"payload": json.dumps({"respuestas": respuestas})},
        headers={"X-Forwarded-For": "2.2.2.2"},
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["ok"] is True
    assert body["respuesta_id"] == 456
    assert body["request_id"]
    assert captured["slug"] == "demo-encuesta"
    assert captured["payload"]["respuestas"] == respuestas
    assert captured["ctx"]["ip"] == "2.2.2.2"
    assert captured["preferred_tenant_id"] == client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"]


def test_responder_parses_respuestas_field_from_form(client, monkeypatch):
    captured: dict = {}

    def fake_save(slug, payload, ctx, **kwargs):
        captured["payload"] = payload
        return _DummyRespuesta(789)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    respuestas = [{"pregunta_id": 5, "texto_libre": "Sí"}]
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data={"respuestas": json.dumps(respuestas), "metadata": json.dumps({"canal": "web"})},
        headers={"X-Forwarded-For": "3.3.3.3"},
    )

    assert response.status_code == 201
    assert response.get_json()["respuesta_id"] == 789
    assert captured["payload"]["respuestas"] == respuestas
    assert captured["payload"]["metadata"] == {"canal": "web"}


def test_responder_flattens_bracketed_form_fields(client, monkeypatch):
    captured: dict = {}

    def fake_save(slug, payload, ctx, **kwargs):
        captured["payload"] = payload
        return _DummyRespuesta(321)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    form_payload = MultiDict(
        [
            ("respuestas[0][pregunta_id]", "10"),
            ("respuestas[0][opcion_ids][]", "2"),
            ("respuestas[0][opcion_ids][]", "3"),
            ("metadata[canal]", "web"),
            ("metadata[demographics][genero]", "femenino"),
        ]
    )

    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data=form_payload,
        headers={"X-Forwarded-For": "4.4.4.4"},
        content_type="application/x-www-form-urlencoded",
    )

    assert response.status_code == 201
    assert response.get_json()["respuesta_id"] == 321

    payload = captured["payload"]
    assert payload["respuestas"] == [
        {"pregunta_id": 10, "opcion_ids": [2, 3]}
    ]
    assert payload["metadata"]["canal"] == "web"
    assert payload["metadata"]["demographics"]["genero"] == "femenino"


def test_responder_duplicate_conflict_is_idempotent_success(client, monkeypatch):
    def fake_save(_slug, _payload, _ctx, **kwargs):
        raise EncuestaError("Ya registramos tu participación", status_code=409)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        json={"respuestas": [{"pregunta_id": 1, "texto_libre": "ok"}]},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "duplicate": True,
        "message": "Ya registramos tu participación",
        "suggested_admin_endpoint_template": "/admin/encuestas/{encuesta_id}/seed-demo/bulk",
    }

def test_share_redirects_to_canonical(client):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = "https://www.chatboc.ar"
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = "https://www.chatboc.ar"

    response = client.get("/e/demo-slug")
    assert response.status_code == 302
    assert response.headers["Location"] == "https://www.chatboc.ar/e/demo-slug"


def test_share_returns_payload_without_canonical(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = None

    monkeypatch.setattr(
        "routes.encuestas_public.get_public_encuesta",
        lambda slug, **_: {"slug": slug},
    )
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get("/e/demo-slug", headers={"Accept": "application/json"})
    assert response.status_code == 200
    assert response.get_json() == {"slug": "demo-slug"}


def test_share_passes_tenant_preference_from_domain_map(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None
    client.application.config["PUBLIC_ENCUESTAS_DOMAIN_MAP"] = {"chatboc.ar": 77}

    captured = {}

    def fake_get(slug, **kwargs):
        captured["slug"] = slug
        captured["preferred_tenant_id"] = kwargs.get("preferred_tenant_id")
        return {"slug": slug}

    monkeypatch.setattr("routes.encuestas_public.get_public_encuesta", fake_get)
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {"slug": slug_publico},
    )

    response = client.get(
        "/e/demo-tenant",
        headers={"Accept": "application/json", "Host": "chatboc.ar"},
    )
    assert response.status_code == 200
    assert captured == {"slug": "demo-tenant", "preferred_tenant_id": 77}


def test_public_error_response_includes_reason_action_and_request_id(client, monkeypatch):
    def fake_get(_slug, **_kwargs):
        raise EncuestaError(
            "La encuesta no está activa",
            status_code=403,
            payload={"reason_code": "survey_not_published"},
        )

    monkeypatch.setattr("routes.encuestas_public.get_public_encuesta", fake_get)

    response = client.get(
        "/public/encuestas/demo-error",
        headers={"X-Request-Id": "req-123"},
    )
    assert response.status_code == 403
    data = response.get_json()
    assert data["reason_code"] == "survey_not_published"
    assert data["retryable"] is False
    assert data["action_hint"] == "view_other_surveys"
    assert data["request_id"] == "req-123"


def test_live_results_unexpected_error_returns_structured_internal_error(client, monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("routes.encuestas_public.calculate_live_results", explode)

    response = client.get("/public/encuestas/demo-slug/live-results")
    assert response.status_code == 500
    data = response.get_json()
    assert data["reason_code"] == "internal_error"
    assert data["retryable"] is True
    assert data["action_hint"] == "retry"
    assert data.get("request_id")


def _create_public_encuesta(slug: str, slug_publico: str, estado: str = "publicada") -> EncEncuesta:
    encuesta = EncEncuesta(
        tenant_id=4,
        slug=slug,
        titulo="Encuesta Demo",
        descripcion="Demo",
        tipo="opinion",
        estado=estado,
    )
    link = EncLink(
        encuesta=encuesta,
        slug_publico=slug_publico,
        canal="web",
    )
    db.session.add(encuesta)
    db.session.add(link)
    db.session.commit()
    return encuesta


def test_share_endpoint_handles_alias_without_link(client):
    slug = "junin-participa"
    slug_publico = "junin-participa-abcdef"
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = None
    _create_public_encuesta(slug, slug_publico)

    response = client.get(f"/e/{slug_publico}", headers={"Accept": "application/json"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug_publico

    link = EncLink.query.filter_by(slug_publico=slug_publico).first()
    db.session.delete(link)
    db.session.commit()

    response = client.get(f"/e/{slug_publico}", headers={"Accept": "application/json"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug_publico

    response = client.get(f"/e/{slug}", headers={"Accept": "application/json"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug


def test_public_encuestas_v1_aliases_resolve_public_slug(client):
    slug = "votacion-en-vivo-luis-petri"
    slug_publico = "votacion-en-vivo-luis-petri"
    client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = 4
    _create_public_encuesta(slug, slug_publico)

    list_response = client.get("/api/public/encuestas/v1")
    assert list_response.status_code == 200
    assert list_response.is_json
    list_payload = list_response.get_json()
    assert list_payload["contract_version"] == "encuestas.public_list.v1"
    assert list_payload["request_id"]
    assert list_payload["items"][0]["contract_version"] == "encuestas.public.v1"

    api_response = client.get(f"/api/public/encuestas/v1/{slug_publico}")
    assert api_response.status_code == 200
    api_payload = api_response.get_json()
    assert api_payload["contract_version"] == "encuestas.public.v1"
    assert api_payload["slug"] == slug_publico
    assert api_payload["request_id"]

    legacy_list_response = client.get("/public/encuestas/v1")
    assert legacy_list_response.status_code == 200
    assert legacy_list_response.is_json
    legacy_list_payload = legacy_list_response.get_json()
    assert legacy_list_payload["contract_version"] == "encuestas.public_list.v1"
    assert legacy_list_payload["items"][0]["slug"] == slug_publico

    legacy_response = client.get(f"/public/encuestas/v1/{slug_publico}")
    assert legacy_response.status_code == 200
    assert legacy_response.get_json()["slug"] == slug_publico


def test_public_encuestas_listing_does_not_bootstrap_demo_data(client, monkeypatch):
    def fail_bootstrap(_tenant_id):
        raise AssertionError("public survey listing must not bootstrap demo data")

    monkeypatch.setattr("services.encuestas_service._bootstrap_sample_if_needed", fail_bootstrap)

    response = client.get("/api/public/encuestas/v1?tenant_id=4")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == "encuestas.public_list.v1"
    assert payload["request_id"]


def test_public_encuestas_v1_missing_slug_returns_public_error_contract(client):
    response = client.get(
        "/api/public/encuestas/v1/votacion-en-vivo-luis-petri",
        headers={"Origin": "https://www.chatboc.ar"},
    )

    assert response.status_code == 404
    assert response.is_json
    payload = response.get_json()
    assert payload["contract_version"] == "public.survey_resolution.v1"
    assert payload["reason_code"] == "survey_not_found"
    assert payload["retryable"] is False
    assert payload["list_endpoint"] == "/api/public/encuestas"
    assert payload["status_code"] == 404
    assert payload["action_hint"] == "go_home"
    assert payload["request_id"]
    assert response.headers["X-Request-Id"] == payload["request_id"]

    legacy_response = client.get("/public/encuestas/v1/votacion-en-vivo-luis-petri")
    assert legacy_response.status_code == 404
    assert legacy_response.get_json()["contract_version"] == "public.survey_resolution.v1"


def test_share_endpoint_renders_accessible_html(client, monkeypatch):
    monkeypatch.setitem(
        client.application.config,
        "PUBLIC_ENCUESTAS_CANONICAL_BASE_URL",
        "https://www.chatboc.ar",
    )
    monkeypatch.setitem(
        client.application.config,
        "PUBLIC_ENCUESTAS_API_BASE_URL",
        "https://api.chatboc.ar",
    )
    monkeypatch.setitem(
        client.application.config,
        "PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL",
        "https://www.chatboc.ar",
    )

    def fake_get(slug, **_):
        encuesta = EncEncuesta(
            tenant_id=4,
            slug=slug,
            titulo="Encuesta Demo",
            descripcion="Descripción corta",
            tipo="opinion",
            estado="publicada",
        )
        return encuesta

    monkeypatch.setattr("routes.encuestas_public.get_public_encuesta", fake_get)
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {
            "slug": slug_publico,
            "titulo": encuesta.titulo,
            "descripcion": encuesta.descripcion,
        },
    )

    response = client.get(
        "/e/demo-slug",
        headers={"Accept": "text/html"},
        base_url="https://www.chatboc.ar",
    )
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Encuestas ciudadanas" in html
    assert "Copiar enlace" in html
    assert "Código QR listo para imprimir" in html
    assert "https://api.chatboc.ar/api/public/encuestas/demo-slug/qr" in html


def test_share_endpoint_uses_default_share_image(client, monkeypatch):
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = None
    client.application.config["PUBLIC_ENCUESTAS_DEFAULT_SHARE_IMAGE_URL"] = "https://cdn.example.com/share.png"

    def fake_get(slug, **_):
        encuesta = EncEncuesta(
            tenant_id=4,
            slug=slug,
            titulo="Encuesta Demo",
            descripcion="Descripción corta",
            tipo="opinion",
            estado="publicada",
        )
        return encuesta

    monkeypatch.setattr("routes.encuestas_public.get_public_encuesta", fake_get)
    monkeypatch.setattr(
        "routes.encuestas_public.serialize_public_encuesta",
        lambda encuesta, slug_publico: {
            "slug": slug_publico,
            "titulo": encuesta.titulo,
            "descripcion": encuesta.descripcion,
        },
    )

    response = client.get("/e/demo-share", headers={"Accept": "text/html"})
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "https://cdn.example.com/share.png" in html
    assert '<meta property="og:image" content="https://cdn.example.com/share.png" />' in html


def test_list_public_encuestas_falls_back_to_slug(client):
    with client.application.app_context():
        encuesta = EncEncuesta(
            tenant_id=4,
            slug="encuesta-sin-link",
            titulo="Participá de la consulta",
            descripcion="Validamos fallback sin link",
            tipo="opinion",
            estado="publicada",
        )
        db.session.add(encuesta)
        db.session.commit()

        resultados = list_public_encuestas_for_tenant(encuesta.tenant_id, limit=10)
        assert any(
            item_encuesta.id == encuesta.id and slug == encuesta.slug
            for item_encuesta, slug in resultados
        )


def test_get_public_encuesta_prefers_active_published_when_link_slug_is_duplicated(client):
    with client.application.app_context():
        shared_public_slug = "movilidad-y-transporte-junin"

        inactive = EncEncuesta(
            tenant_id=4,
            slug="movilidad-y-transporte-junin-legacy",
            titulo="Encuesta vieja",
            descripcion="Encuesta cerrada",
            tipo="opinion",
            estado="borrador",
        )
        inactive_link = EncLink(
            encuesta=inactive,
            slug_publico=shared_public_slug,
            canal="web",
        )

        active = EncEncuesta(
            tenant_id=4,
            slug="movilidad-y-transporte-junin-vigente",
            titulo="Encuesta vigente",
            descripcion="Encuesta activa",
            tipo="opinion",
            estado="publicada",
        )
        active_link = EncLink(
            encuesta=active,
            slug_publico=shared_public_slug,
            canal="web",
        )

        db.session.add_all([inactive, inactive_link, active, active_link])
        db.session.commit()

        resolved = get_public_encuesta(shared_public_slug)

        assert resolved.id == active.id
        assert resolved.estado == "publicada"


def test_get_public_encuesta_prefers_requested_tenant_when_slug_is_shared(client):
    with client.application.app_context():
        shared_public_slug = "votacion-en-vivo-rio-grande"

        now = datetime.utcnow()
        wrong_tenant = EncEncuesta(
            tenant_id=7,
            slug="votacion-en-vivo-rio-grande-legacy",
            titulo="Encuesta cerrada en otro tenant",
            descripcion="No debería resolverse para tenant 4",
            tipo="opinion",
            estado="publicada",
            inicio_at=now - timedelta(days=10),
            fin_at=now - timedelta(days=1),
        )
        wrong_tenant_link = EncLink(
            encuesta=wrong_tenant,
            slug_publico=shared_public_slug,
            canal="web",
        )

        expected = EncEncuesta(
            tenant_id=4,
            slug="votacion-en-vivo-rio-grande-actual",
            titulo="Encuesta activa",
            descripcion="Debe mostrarse para tenant 4",
            tipo="opinion",
            estado="publicada",
            inicio_at=now - timedelta(days=1),
            fin_at=now + timedelta(days=10),
        )
        expected_link = EncLink(
            encuesta=expected,
            slug_publico=shared_public_slug,
            canal="web",
        )

        db.session.add_all([wrong_tenant, wrong_tenant_link, expected, expected_link])
        db.session.commit()

        resolved = get_public_encuesta(shared_public_slug, preferred_tenant_id=4)

        assert resolved.id == expected.id
        assert resolved.tenant_id == 4


def test_qr_endpoint_returns_png_for_public_encuesta(client):
    slug = "encuesta-qr-publica"
    slug_publico = f"{slug}-abcdef"
    _create_public_encuesta(slug, slug_publico)

    response = client.get(f"/api/public/encuestas/{slug_publico}/qr")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data  # Non-empty payload


def test_qr_endpoint_allows_preview_for_authorized_user(client):
    slug = "encuesta-qr-preview"
    slug_publico = f"{slug}-123abc"
    encuesta = _create_public_encuesta(slug, slug_publico, estado="borrador")

    # Public access should be rejected while the survey is unpublished.
    blocked = client.get(f"/api/public/encuestas/{slug_publico}/qr")
    assert blocked.status_code == 403

    admin = User(
        email="preview-admin@example.com",
        name="Preview Admin",
        rol="admin",
        municipio_id=encuesta.tenant_id,
        tipo_chat="municipio",
    )
    admin.set_password("demo1234")
    db.session.add(admin)
    db.session.commit()

    login = client.post(
        "/auth/login",
        json={"email": admin.email, "password": "demo1234"},
    )
    assert login.status_code == 200
    token = login.get_json()["token"]

    headers = {"Authorization": f"Bearer {token}"}
    preview = client.get(
        f"/api/public/encuestas/{slug_publico}/qr",
        headers=headers,
    )
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert preview.data


def test_qr_endpoint_allows_preview_with_session_user(client, monkeypatch):
    slug = "encuesta-qr-session-preview"
    slug_publico = f"{slug}-xyz987"
    encuesta = _create_public_encuesta(slug, slug_publico, estado="borrador")

    session_admin = User(
        email="session-admin@example.com",
        name="Session Admin",
        rol="admin",
        municipio_id=encuesta.tenant_id,
        tipo_chat="municipio",
    )
    session_admin.set_password("demo1234")
    db.session.add(session_admin)
    db.session.commit()

    monkeypatch.setattr(
        "routes.encuestas_public._resolve_preview_user",
        lambda: session_admin,
    )

    preview = client.get(f"/api/public/encuestas/{slug_publico}/qr")
    assert preview.status_code == 200
    assert preview.mimetype == "image/png"
    assert preview.data


@pytest.fixture(autouse=True)
def restore_canonical_base(client):
    original = client.application.config.get("PUBLIC_ENCUESTAS_CANONICAL_BASE_URL")
    yield
    client.application.config["PUBLIC_ENCUESTAS_CANONICAL_BASE_URL"] = original
