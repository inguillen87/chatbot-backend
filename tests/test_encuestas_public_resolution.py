from database import db
from datetime import datetime, timedelta
import json

import pytest
from werkzeug.datastructures import MultiDict

from models import (
    EncEncuesta,
    EncLink,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    EncRespuestaDetalle,
    TenantProfile,
    User,
)
from routes import encuestas_public
from services.encuestas_service import EncuestaError, get_public_encuesta, list_public_encuestas_for_tenant
from services.demo_surveys import build_demo_survey_chat_menu, build_demo_surveys_votings_contract
from services.response_formatter import build_interactive_response


@pytest.fixture(autouse=True)
def _canonical_public_tenant_profiles(client):
    """Public contracts now resolve every numeric scope through TenantProfile."""

    for tenant_id in (4, 5, 7, 11, 77):
        owner = User(
            id=10000 + tenant_id,
            email=f"public-tenant-{tenant_id}@example.com",
            name=f"Public Tenant {tenant_id}",
            password_hash="hash",
            rol="admin",
            tipo_chat="municipio",
        )
        tenant = TenantProfile(
            id=tenant_id,
            slug=f"public-tenant-{tenant_id}",
            nombre=f"Public Tenant {tenant_id}",
            tipo="municipio",
            municipio_id=owner.id,
        )
        db.session.add_all([owner, tenant])
    db.session.commit()


class _DummyRespuesta:
    def __init__(self, respuesta_id):
        self.id = respuesta_id


def _submission_contract(payload, suffix: str):
    """Attach durable HTTP identity explicitly instead of mutating the client fixture."""

    submission_id = f"test-public-survey-{suffix}"
    if isinstance(payload, MultiDict):
        body = payload.copy()
        body.setlist("submission_id", [submission_id])
    else:
        body = dict(payload)
        body["submission_id"] = submission_id
    return body, {"Idempotency-Key": submission_id}


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
    assert data[0]["url_publica"] == (
        "https://www.chatboc.ar/e/slug-demo?tenant_slug=public-tenant-5"
    )
    assert data[0]["tenant_slug"] == "public-tenant-5"


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
    assert data[0]["url_publica"] == (
        "https://participa.junin.ar/e/slug-demo?tenant_slug=public-tenant-5"
    )


def test_public_demo_surveys_list_uses_seeded_contract(client):
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = "https://www.chatboc.ar"

    response = client.get(
        "/api/public/encuestas/v1?tenant_slug=qa-colegio-sandbox&sector=educacion&demo_mode=1"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["demo_mode"] is True
    assert payload["count"] == 5
    assert payload["seed_policy"]["responses_per_item"] == 100
    assert all(item["demo_mode"] is True for item in payload["items"])
    assert all(item["whatsapp_share_url"].startswith("https://wa.me/") for item in payload["items"])


def test_public_demo_survey_detail_results_and_response(client):
    client.application.config["PUBLIC_ENCUESTAS_QR_TARGET_BASE_URL"] = "https://www.chatboc.ar"
    contract = build_demo_surveys_votings_contract(
        sector="empresas",
        tenant_slug="bodega",
        public_base_url="https://www.chatboc.ar",
    )
    slug = contract["items"][0]["slug"]

    detail = client.get(f"/api/public/encuestas/v1/{slug}")
    assert detail.status_code == 200
    detail_payload = detail.get_json()
    assert detail_payload["demo_mode"] is True
    assert detail_payload["resultados_envivo"]["total_respuestas"] == 100
    assert detail_payload["preguntas"]
    question = detail_payload["preguntas"][0]
    option = question["opciones"][0]

    results = client.get(f"/api/public/encuestas/v1/{slug}/live-results")
    assert results.status_code == 200
    results_payload = results.get_json()
    assert results_payload["demo_mode"] is True
    assert results_payload["total_respuestas"] == 100
    assert results_payload["heatmap"]["points"]

    submitted = client.post(
        f"/api/public/encuestas/v1/{slug}/responder",
        json={
            "answers": [{"question_id": question["id"], "option_id": option["id"]}],
            "metadata": {"source": "pytest"},
        },
    )
    assert submitted.status_code == 201
    submitted_payload = submitted.get_json()
    assert submitted_payload["demo_mode"] is True
    assert submitted_payload["contract_version"] == "demo.survey_response_ack.v1"
    assert submitted_payload["accepted"] is True
    assert submitted_payload["answer_count"] == 1
    assert submitted_payload["answers"][0]["question_id"] == question["id"]
    assert submitted_payload["answers"][0]["option_id"] == option["id"]
    assert submitted_payload["seeded_responses_before"] == 100
    assert submitted_payload["resultados_envivo"]["total_respuestas"] == 100
    assert submitted_payload["results_endpoint"].endswith("/live-results")
    assert f"/e/{slug}" in submitted_payload["next_url"]

    submitted_label = client.post(
        f"/api/public/encuestas/v1/{slug}/responder",
        json={"respuestas": [{"pregunta_id": question["id"], "opcion": option["texto"]}]},
    )
    assert submitted_label.status_code == 201
    submitted_label_payload = submitted_label.get_json()
    assert submitted_label_payload["accepted"] is True
    assert submitted_label_payload["answers"][0]["option_id"] == option["id"]

    submitted_option_ids = client.post(
        f"/api/public/encuestas/v1/{slug}/responder",
        json={"respuestas": [{"pregunta_id": question["id"], "opcion_ids": [option["id"]]}]},
    )
    assert submitted_option_ids.status_code == 201
    submitted_option_ids_payload = submitted_option_ids.get_json()
    assert submitted_option_ids_payload["accepted"] is True
    assert submitted_option_ids_payload["answers"][0]["option_id"] == option["id"]


def test_demo_survey_chat_menu_lists_five_with_whatsapp_vote_actions():
    menu = build_demo_survey_chat_menu(
        sector="gobierno",
        tenant_slug="junin-1",
        channel="whatsapp",
        public_base_url="https://www.chatboc.ar",
    )

    assert menu["contract_version"] == "demo.encuestas_menu.v1"
    assert menu["pagination"]["page_size"] == 5
    assert len(menu["surveys"]) == 5
    assert "Elegi una encuesta" in menu["message_body"]
    assert "Compartir: https://wa.me/" not in menu["message_body"]
    assert len(menu["message_body"]) < 1400
    assert all((survey.get("seed") or {}).get("responses") == 100 for survey in menu["surveys"])
    assert menu["pagination"]["next_action_id"] == "mostrar_menu_encuestas::2"

    formatted = build_interactive_response(
        options=menu["options_list"],
        body_text=menu["message_body"],
        channel="whatsapp",
        message_type=menu["message_type"],
        original_bot_response=menu,
    )
    formatted_body = formatted["text"]["body"]
    context_options = (formatted.get("contexto_actualizado") or {}).get("last_options_sent") or []
    action_ids = [str(option.get("action_id") or "") for option in context_options]
    assert len(formatted_body) < 1600
    assert "Responde con el numero de la encuesta" in formatted_body
    assert formatted_body.count("Votar encuesta") == 5
    assert "Compartir 1" not in formatted_body
    assert "Compartir: https://wa.me/" not in formatted_body
    assert any(option.get("action_id") == "mostrar_menu_encuestas::2" for option in context_options)
    assert any(action.startswith("chatboc_survey_open::") for action in action_ids)
    assert not any(action.startswith("chatboc_survey_share::") for action in action_ids)


def test_respuestas_alias_reuses_handler(client, monkeypatch):
    saved_calls = {}
    trusted_peer = "192.0.2.41"
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_TYPE", raising=False)

    def fake_save(slug, payload, ctx, **kwargs):
        saved_calls["slug"] = slug
        saved_calls["payload"] = payload
        saved_calls["ctx"] = ctx
        saved_calls["preferred_tenant_id"] = kwargs.get("preferred_tenant_id")
        return _DummyRespuesta(123)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    payload, idempotency_headers = _submission_contract(
        {"respuesta": "ok"},
        "alias-0001",
    )
    response = client.post(
        "/public/encuestas/demo-encuesta/respuestas",
        json=payload,
        headers={
            **idempotency_headers,
            "X-Forwarded-For": "198.51.100.10",
            "CF-Connecting-IP": "203.0.113.10",
        },
        environ_overrides={"REMOTE_ADDR": trusted_peer},
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["contract_version"] == "encuestas.public_response.v1"
    assert body["ok"] is True
    assert body["success"] is True
    assert body["respuesta_id"] == 123
    assert body["request_id"]
    assert saved_calls["slug"] == "demo-encuesta"
    assert saved_calls["payload"] == payload
    assert saved_calls["ctx"]["ip"] == trusted_peer
    assert saved_calls["preferred_tenant_id"] == client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"]


def test_responder_accepts_form_payload(client, monkeypatch):
    captured: dict = {}
    trusted_peer = "192.0.2.42"
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_TYPE", raising=False)

    def fake_save(slug, payload, ctx, **kwargs):
        captured["slug"] = slug
        captured["payload"] = payload
        captured["ctx"] = ctx
        captured["preferred_tenant_id"] = kwargs.get("preferred_tenant_id")
        return _DummyRespuesta(456)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    respuestas = [{"pregunta_id": 10, "opcion_ids": [2]}]
    form_data, idempotency_headers = _submission_contract(
        {"payload": json.dumps({"respuestas": respuestas})},
        "form-payload-0001",
    )
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data=form_data,
        headers={
            **idempotency_headers,
            "X-Forwarded-For": "198.51.100.20",
            "CF-Connecting-IP": "203.0.113.20",
        },
        environ_overrides={"REMOTE_ADDR": trusted_peer},
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["contract_version"] == "encuestas.public_response.v1"
    assert body["ok"] is True
    assert body["success"] is True
    assert body["respuesta_id"] == 456
    assert body["request_id"]
    assert captured["slug"] == "demo-encuesta"
    assert captured["payload"]["respuestas"] == respuestas
    assert captured["ctx"]["ip"] == trusted_peer
    assert captured["preferred_tenant_id"] == client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"]


def test_public_response_ip_ignores_rotated_forwarding_headers_outside_render(client, monkeypatch):
    trusted_peer = "192.0.2.43"
    captured_ips: list[str] = []
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_TYPE", raising=False)

    def fake_save(_slug, _payload, ctx, **_kwargs):
        captured_ips.append(ctx["ip"])
        return _DummyRespuesta(800 + len(captured_ips))

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    for suffix, forwarded_ip, cloudflare_ip in (
        ("rotated-0001", "198.51.100.31", "203.0.113.31"),
        ("rotated-0002", "198.51.100.32", "203.0.113.32"),
    ):
        payload, idempotency_headers = _submission_contract(
            {"respuesta": "ok"},
            suffix,
        )
        response = client.post(
            "/public/encuestas/demo-encuesta/respuestas",
            json=payload,
            headers={
                **idempotency_headers,
                "X-Forwarded-For": forwarded_ip,
                "CF-Connecting-IP": cloudflare_ip,
            },
            environ_overrides={"REMOTE_ADDR": trusted_peer},
        )
        assert response.status_code == 201

    assert captured_ips == [trusted_peer, trusted_peer]


def test_responder_parses_respuestas_field_from_form(client, monkeypatch):
    captured: dict = {}

    def fake_save(slug, payload, ctx, **kwargs):
        captured["payload"] = payload
        return _DummyRespuesta(789)

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    respuestas = [{"pregunta_id": 5, "texto_libre": "Sí"}]
    form_data, idempotency_headers = _submission_contract(
        {"respuestas": json.dumps(respuestas), "metadata": json.dumps({"canal": "web"})},
        "form-fields-0001",
    )
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data=form_data,
        headers={**idempotency_headers, "X-Forwarded-For": "3.3.3.3"},
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["contract_version"] == "encuestas.public_response.v1"
    assert body["respuesta_id"] == 789
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
    form_payload, idempotency_headers = _submission_contract(
        form_payload,
        "bracketed-form-0001",
    )

    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        data=form_payload,
        headers={**idempotency_headers, "X-Forwarded-For": "4.4.4.4"},
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


def test_responder_duplicate_conflict_never_fabricates_a_durable_ack(client, monkeypatch):
    def fake_save(_slug, _payload, _ctx, **kwargs):
        raise EncuestaError(
            "Ya registramos tu participación",
            status_code=409,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "survey_response_duplicate",
                "retryable": False,
                "action_hint": "show_existing_participation",
            },
        )

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    payload, idempotency_headers = _submission_contract(
        {"respuestas": [{"pregunta_id": 1, "texto_libre": "ok"}]},
        "duplicate-0001",
    )
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        json=payload,
        headers=idempotency_headers,
    )

    assert response.status_code == 409
    response_payload = response.get_json()
    assert response_payload["reason_code"] == "survey_response_duplicate"
    assert response_payload["retryable"] is False
    assert "response_id" not in response_payload
    assert response_payload.get("persisted") is not True


def test_responder_non_duplicate_conflict_remains_conflict(client, monkeypatch):
    def fake_save(_slug, _payload, _ctx, **kwargs):
        raise EncuestaError(
            "La encuesta cambió desde que abriste el formulario",
            status_code=409,
            payload={
                "contract_version": "surveys.public_response.v2",
                "reason_code": "survey_structure_changed",
                "retryable": False,
                "action_hint": "reload_survey",
            },
        )

    monkeypatch.setattr("routes.encuestas_public.save_respuesta", fake_save)

    payload, idempotency_headers = _submission_contract(
        {"respuestas": [{"pregunta_id": 1, "texto_libre": "ok"}]},
        "conflict-0001",
    )
    response = client.post(
        "/public/encuestas/demo-encuesta/responder",
        json=payload,
        headers=idempotency_headers,
    )

    assert response.status_code == 409
    assert response.get_json()["reason_code"] == "survey_structure_changed"
    assert response.get_json()["retryable"] is False

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
    payload = response.get_json()
    assert payload["slug"] == "demo-slug"
    assert payload["tenant_slug"] == "public-tenant-5"
    assert payload["url_publica"].endswith(
        "/e/demo-slug?tenant_slug=public-tenant-5"
    )


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


def test_public_detail_reports_canonical_slug_when_loaded_from_base_slug(client):
    slug = "luis-petri-votacion-prioridades-junin-8887"
    slug_publico = f"{slug}-1c5aa4"
    client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = 4
    _create_public_encuesta(slug, slug_publico)

    response = client.get(f"/api/public/encuestas/v1/{slug}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["slug"] == slug
    assert payload["slug_publico"] == slug_publico
    assert payload["canonical_slug"] == slug_publico
    assert payload["requested_slug"] == slug
    assert payload["slug_alias_used"] is True
    assert payload["url_publica"].endswith(
        f"/e/{slug_publico}?tenant_slug=public-tenant-4"
    )
    assert payload["public_api_endpoint"].endswith(
        f"/{slug_publico}?tenant_slug=public-tenant-4"
    )


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


@pytest.mark.parametrize(
    "query",
    [
        "tenant_slug=no-existe",
        "tenant_id=4&tenant_slug=public-tenant-5",
    ],
)
def test_public_listing_rejects_invalid_or_contradictory_explicit_scope(
    client,
    query,
):
    client.application.config["PUBLIC_ENCUESTAS_DEFAULT_TENANT_ID"] = 5

    response = client.get(f"/api/public/encuestas/v1?{query}")

    assert response.status_code == 404
    payload = response.get_json()
    assert payload["reason_code"] == "survey_not_found"
    assert payload["contract_version"] == "public.survey_resolution.v1"


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


def test_public_encuestas_listing_uses_lightweight_summary(client, monkeypatch):
    with client.application.app_context():
        encuesta = EncEncuesta(
            tenant_id=4,
            slug="consulta-liviana",
            titulo="Consulta liviana",
            descripcion="No debe cargar preguntas ni resultados",
            tipo="opinion",
            estado="publicada",
            mostrar_resultados_envivo=True,
        )
        db.session.add(encuesta)
        db.session.commit()

    def explode_serializer(*_args, **_kwargs):
        raise AssertionError("public listing must not use the detail serializer")

    monkeypatch.setattr("routes.encuestas_public.serialize_public_encuesta", explode_serializer)

    response = client.get("/api/public/encuestas/v1?tenant_id=4")
    assert response.status_code == 200
    data = response.get_json()
    assert data["contract_version"] == "encuestas.public_list.v1"
    item = next(item for item in data["items"] if item["slug"] == "consulta-liviana")
    assert item["contract_version"] == "encuestas.public.v1"
    assert item["titulo"] == "Consulta liviana"
    assert item["tenant_slug"] == "public-tenant-4"
    assert data["tenant_slug"] == "public-tenant-4"
    assert item["url_publica"].endswith(
        "/e/consulta-liviana?tenant_slug=public-tenant-4"
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


def test_live_results_uses_tenant_slug_when_public_slug_is_shared(client):
    with client.application.app_context():
        shared_public_slug = "consulta-live-compartida"
        owner_a = User(
            email="owner-live-a@example.com",
            name="Owner Live A",
            password_hash="x",
            rol="admin",
            tipo_chat="municipio",
        )
        owner_b = User(
            email="owner-live-b@example.com",
            name="Owner Live B",
            password_hash="x",
            rol="admin",
            tipo_chat="municipio",
        )
        db.session.add_all([owner_a, owner_b])
        db.session.flush()
        tenant_a = TenantProfile(
            slug="tenant-live-a",
            nombre="Tenant Live A",
            tipo="municipio",
            municipio_id=owner_a.id,
        )
        tenant_b = TenantProfile(
            slug="tenant-live-b",
            nombre="Tenant Live B",
            tipo="municipio",
            municipio_id=owner_b.id,
        )
        db.session.add_all([tenant_a, tenant_b])
        db.session.flush()

        def create_live_survey(tenant_id: int, slug: str, label: str, votes: int) -> EncEncuesta:
            encuesta = EncEncuesta(
                tenant_id=tenant_id,
                slug=slug,
                titulo=f"Encuesta {label}",
                descripcion="Live",
                tipo="votacion",
                estado="publicada",
                es_votacion_envivo=True,
                mostrar_resultados_envivo=True,
            )
            pregunta = EncPregunta(encuesta=encuesta, orden=1, tipo="opcion_unica", texto="Prioridad")
            opcion = EncOpcion(pregunta=pregunta, orden=1, texto=label, valor=label.lower())
            link = EncLink(encuesta=encuesta, slug_publico=shared_public_slug, canal="web")
            db.session.add_all([encuesta, pregunta, opcion, link])
            db.session.flush()
            for index in range(votes):
                respuesta = EncRespuesta(
                    encuesta=encuesta,
                    tenant_id=tenant_id,
                    huella_unica=f"{slug}-{index}",
                    canal="web",
                )
                detalle = EncRespuestaDetalle(respuesta=respuesta, pregunta=pregunta, opcion=opcion)
                db.session.add_all([respuesta, detalle])
            return encuesta

        create_live_survey(tenant_a.id, "consulta-live-tenant-a", "A", 1)
        create_live_survey(tenant_b.id, "consulta-live-tenant-b", "B", 3)
        db.session.commit()

    response = client.get(
        f"/api/public/encuestas/v1/{shared_public_slug}/live-results?tenant_slug=tenant-live-b&include_heatmap=0"
    )

    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["slug_publico"] == shared_public_slug
    assert data["total_respuestas"] == 3
    first_question = data["preguntas"][0]
    assert first_question["total_votos"] == 3
    assert first_question["opciones"][0]["texto"] == "B"


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
        tenant_id=encuesta.tenant_id,
        municipio_id=10000 + encuesta.tenant_id,
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
        tenant_id=encuesta.tenant_id,
        municipio_id=10000 + encuesta.tenant_id,
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
