import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Response

from app import db
from models import (
    MunicipioTicket,
    PlantillasRespuesta,
    PymeTicket,
    TenantProfile,
    TenantTicket,
    User,
)
from services.ai_response_templates import (
    AI_TEMPLATES_CONTRACT_VERSION,
    AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
    AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
    MAX_GENERATE_TEMPLATE_REQUEST_BYTES,
    MAX_IMPROVE_TEMPLATE_REQUEST_BYTES,
    MAX_PREVIEW_REQUEST_BYTES,
    MAX_SUGGESTION_REQUEST_BYTES,
    MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
    MAX_TOP_N,
)


def _headers(app, user: User, tenant_slug: str | None = None) -> dict[str, str]:
    token = jwt.encode(
        {
            "user_id": user.id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        app.config["SECRET_KEY"],
        algorithm="HS256",
    )
    headers = {"Authorization": f"Bearer {token}"}
    if tenant_slug:
        headers["X-Tenant-Slug"] = tenant_slug
    return headers


def _request_without_content_length(
    app,
    *,
    path: str,
    method: str,
    headers: dict[str, str],
    payload: dict,
) -> tuple[Response, int]:
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    builder = EnvironBuilder(
        path=path,
        method=method,
        headers=headers,
        content_type="application/json",
        input_stream=BytesIO(raw_body),
    )
    environ = builder.get_environ()
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True
    counting_stream = _CountingInput(raw_body)
    environ["wsgi.input"] = counting_stream
    response = Response.from_app(app.wsgi_app, environ)
    return response, counting_stream.bytes_read


def _payload_with_exact_json_size(target_bytes: int) -> dict[str, str]:
    payload = {"asunto": "a", "p": ""}
    empty_size = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    assert empty_size <= target_bytes
    payload["p"] = "x" * (target_bytes - empty_size)
    assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) == target_bytes
    return payload


class _CountingInput(BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        return chunk

    def readinto(self, buffer) -> int:
        size = super().readinto(buffer)
        self.bytes_read += int(size or 0)
        return size


def _tenant(label: str, *, role: str = "admin") -> tuple[User, TenantProfile]:
    owner = User(
        email=f"ai-contract-{label}@test.com",
        name=f"AI Contract {label}",
        rol=role,
        tipo_chat="municipio",
    )
    owner.set_password("pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"ai-contract-{label}",
        nombre=f"Municipio {label}",
        tipo="municipio",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    owner.tenant_slug = tenant.slug
    db.session.commit()
    return owner, tenant


def _template(
    name: str,
    text: str,
    *,
    tenant_id: int | None = None,
    active: bool = True,
    embedding: list[float] | None = None,
) -> PlantillasRespuesta:
    row = PlantillasRespuesta(
        tenant_id=tenant_id,
        name=name,
        text=text,
        keywords=[],
        is_active=active,
        embedding=embedding,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _municipio_ticket(
    tenant: TenantProfile,
    *,
    category: str = "Alumbrado",
    customer_name: str = "Ana Ciudadana",
) -> MunicipioTicket:
    row = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        pregunta="Contenido ciudadano que no debe entrar en logs ni variables arbitrarias.",
        asunto="Luminaria sin funcionar",
        categoria=category,
        estado="en_proceso",
        nombre_vecino=customer_name,
        direccion="Calle Segura 123",
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_management_listing_preserves_inactive_and_picker_filter_is_strict_and_scoped(
    client,
    app,
):
    owner_a, tenant_a = _tenant("listing-a")
    _owner_b, tenant_b = _tenant("listing-b")
    global_active = _template("Global activa", "Base global")
    tenant_active = _template("Tenant activa", "Base A", tenant_id=tenant_a.id)
    tenant_inactive = _template(
        "Tenant inactiva",
        "Base A inactiva",
        tenant_id=tenant_a.id,
        active=False,
    )
    _template("Tenant B", "Privada B", tenant_id=tenant_b.id)
    headers = _headers(app, owner_a, tenant_a.slug)

    management = client.get("/api/ai/templates", headers=headers)
    assert management.status_code == 200
    management_payload = management.get_json()
    assert management_payload["contract_version"] == AI_TEMPLATES_CONTRACT_VERSION
    assert {item["id"] for item in management_payload["plantillas"]} == {
        global_active.id,
        tenant_active.id,
        tenant_inactive.id,
    }
    global_item = next(
        item for item in management_payload["plantillas"] if item["id"] == global_active.id
    )
    assert global_item["readonly"] is True
    assert global_item["editable"] is False

    picker = client.get("/api/ai/templates?active_only=1", headers=headers)
    assert picker.status_code == 200
    assert {item["id"] for item in picker.get_json()["plantillas"]} == {
        global_active.id,
        tenant_active.id,
    }

    invalid = client.get("/api/ai/templates?active_only=maybe", headers=headers)
    assert invalid.status_code == 400
    assert invalid.get_json()["reason_code"] == "active_only_invalid"


def test_global_templates_are_readonly_without_mutation_or_embedding(client, app):
    owner, tenant = _tenant("global-readonly")
    global_template = _template("Global protegida", "No modificar")
    headers = _headers(app, owner, tenant.slug)

    with patch("routes.ai_templates.embed_textos_llm") as embed:
        update = client.put(
            f"/api/ai/templates/{global_template.id}",
            headers=headers,
            json={"text": "Intento de cambio"},
        )
    assert update.status_code == 403
    assert update.get_json()["reason_code"] == "global_template_readonly"
    embed.assert_not_called()
    assert db.session.get(PlantillasRespuesta, global_template.id).text == "No modificar"

    delete = client.delete(
        f"/api/ai/templates/{global_template.id}",
        headers=headers,
    )
    assert delete.status_code == 403
    assert delete.get_json()["reason_code"] == "global_template_readonly"
    assert db.session.get(PlantillasRespuesta, global_template.id) is not None


def test_suggestions_enforce_utf8_limits_top_n_and_active_tenant_scope(client, app):
    owner_a, tenant_a = _tenant("suggest-a")
    _owner_b, tenant_b = _tenant("suggest-b")
    embedding = [1.0, 0.0]
    tenant_template = _template(
        "Tenant A activa",
        "Respuesta A",
        tenant_id=tenant_a.id,
        embedding=embedding,
    )
    global_template = _template(
        "Global activa",
        "Respuesta global",
        embedding=embedding,
    )
    _template(
        "Tenant A inactiva",
        "No sugerir",
        tenant_id=tenant_a.id,
        active=False,
        embedding=embedding,
    )
    _template(
        "Tenant B privada",
        "No cruzar",
        tenant_id=tenant_b.id,
        embedding=embedding,
    )
    headers = _headers(app, owner_a, tenant_a.slug)

    with patch("routes.ai.embed_textos_llm", return_value=[embedding]) as embed:
        response = client.post(
            "/api/ai/suggest-templates",
            headers=headers,
            json={"asunto": "Alumbrado", "top_n": 2},
        )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["contract_version"] == AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION
    assert [item["id_plantilla"] for item in payload["sugerencias"]] == [
        tenant_template.id,
        global_template.id,
    ]
    embed.assert_called_once()

    with patch("routes.ai.embed_textos_llm") as embed:
        too_many = client.post(
            "/api/ai/suggest-templates",
            headers=headers,
            json={"asunto": "Alumbrado", "top_n": MAX_TOP_N + 1},
        )
    assert too_many.status_code == 400
    assert too_many.get_json()["reason_code"] == "top_n_exceeds_limit"
    embed.assert_not_called()

    utf8_subject = json.dumps(
        {"asunto": "á" * 257},
        ensure_ascii=False,
    )
    with patch("routes.ai.embed_textos_llm") as embed:
        oversized_subject = client.post(
            "/api/ai/suggest-templates",
            headers={**headers, "Content-Type": "application/json"},
            data=utf8_subject.encode("utf-8"),
        )
    assert oversized_subject.status_code == 413
    assert oversized_subject.get_json()["reason_code"] == "asunto_too_long"
    embed.assert_not_called()

    utf8_context = json.dumps(
        {"asunto": "Alumbrado", "contexto_ticket": "😀" * 2049},
        ensure_ascii=False,
    )
    with patch("routes.ai.embed_textos_llm") as embed:
        oversized_context = client.post(
            "/api/ai/suggest-templates",
            headers={**headers, "Content-Type": "application/json"},
            data=utf8_context.encode("utf-8"),
        )
    assert oversized_context.status_code == 413
    assert oversized_context.get_json()["reason_code"] == "contexto_ticket_too_long"
    embed.assert_not_called()


def test_template_text_utf8_rejection_has_no_database_or_embedding_side_effect(
    client,
    app,
):
    owner, tenant = _tenant("oversize-create")
    headers = _headers(app, owner, tenant.slug)
    before = PlantillasRespuesta.query.count()
    request_body = json.dumps(
        {"name": "Respuesta", "text": "😀" * 4097, "keywords": []},
        ensure_ascii=False,
    )

    with patch("routes.ai_templates.embed_textos_llm") as embed:
        response = client.post(
            "/api/ai/templates",
            headers={**headers, "Content-Type": "application/json"},
            data=request_body.encode("utf-8"),
        )

    assert response.status_code == 413
    assert response.get_json()["reason_code"] == "text_too_long"
    embed.assert_not_called()
    assert PlantillasRespuesta.query.count() == before

    private_name = "Respuesta DNI 32877851"
    private_text = "Contenido privado 32877851 que no debe registrarse"
    provider_detail = "provider-private-32877851"
    with patch.object(app.logger, "info") as info_log, patch.object(
        app.logger,
        "warning",
    ) as warning_log, patch.object(app.logger, "error") as error_log, patch(
        "routes.ai_templates.embed_textos_llm",
        side_effect=RuntimeError(provider_detail),
    ):
        created_without_embedding = client.post(
            "/api/ai/templates",
            headers=headers,
            json={"name": private_name, "text": private_text, "keywords": []},
        )

    assert created_without_embedding.status_code == 201
    log_calls = (
        info_log.call_args_list
        + warning_log.call_args_list
        + error_log.call_args_list
    )
    rendered_logs = "\n".join(
        call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0]
        for call in log_calls
    )
    assert private_name not in rendered_logs
    assert private_text not in rendered_logs
    assert provider_detail not in rendered_logs
    assert "error_type=RuntimeError" in rendered_logs


def test_ticket_preview_is_server_rendered_tenant_scoped_and_has_no_side_effects(
    client,
    app,
):
    owner_a, tenant_a = _tenant("preview-a")
    _owner_b, tenant_b = _tenant("preview-b")
    ticket_a = _municipio_ticket(tenant_a)
    ticket_b = _municipio_ticket(tenant_b, customer_name="Dato tenant B")
    template = _template(
        "Actualización global",
        "Hola {{ nombre_cliente }}. El caso {{nro_ticket}} está {{estado}}.",
    )
    private_b_template = _template(
        "Privada B",
        "Contenido que no debe cruzar tenants",
        tenant_id=tenant_b.id,
    )
    headers = _headers(app, owner_a, tenant_a.slug)
    before = (
        PlantillasRespuesta.query.count(),
        MunicipioTicket.query.count(),
    )

    response = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": template.id,
            "ticket_id": ticket_a.id,
            "source_model": "MunicipioTicket",
        },
    )
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    payload = response.get_json()
    assert payload["contract_version"] == AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION
    assert payload["rendered_text"] == (
        f"Hola Ana Ciudadana. El caso {ticket_a.nro_ticket} está en_proceso."
    )
    assert payload["unresolved_variables"] == []
    assert payload["readiness"] == {
        "ready_to_insert": True,
        "server_rendered": True,
        "blocker": None,
    }
    assert payload["rendering_policy"]["client_interpolation_allowed"] is False
    assert payload["side_effects"]["records_written"] == 0

    cross_tenant = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": template.id,
            "ticket_id": ticket_b.id,
            "source_model": "MunicipioTicket",
        },
    )
    assert cross_tenant.status_code == 404
    assert cross_tenant.get_json()["reason_code"] == "ticket_not_found"
    assert "Dato tenant B" not in cross_tenant.get_data(as_text=True)

    cross_template = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": private_b_template.id,
            "ticket_id": ticket_a.id,
            "source_model": "MunicipioTicket",
        },
    )
    assert cross_template.status_code == 404
    assert cross_template.get_json()["reason_code"] == "template_not_found"
    assert "Contenido que no debe cruzar tenants" not in cross_template.get_data(as_text=True)
    assert before == (
        PlantillasRespuesta.query.count(),
        MunicipioTicket.query.count(),
    )


def test_ticket_preview_blocks_unresolved_variables_and_employee_category_access(
    client,
    app,
):
    owner, tenant = _tenant("preview-rbac")
    ticket = _municipio_ticket(tenant, category="Alumbrado")
    unresolved_template = _template(
        "Variable gobernada",
        "Caso {{nro_ticket}}: {{variable_no_autorizada}}",
        tenant_id=tenant.id,
    )
    employee = User(
        email="ai-contract-employee@test.com",
        name="Operador restringido",
        rol="empleado",
        es_empleado=True,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        empresa_id=owner.id,
        ticket_categorias="Arbolado",
        tipo_chat="municipio",
    )
    employee.set_password("pass")
    db.session.add(employee)
    db.session.commit()
    request_body = {
        "template_id": unresolved_template.id,
        "ticket_id": ticket.id,
        "source_model": "MunicipioTicket",
    }

    denied = client.post(
        "/api/ai/templates/ticket-preview",
        headers=_headers(app, employee, tenant.slug),
        json=request_body,
    )
    assert denied.status_code == 404
    assert denied.get_json()["reason_code"] == "ticket_not_found"

    allowed = client.post(
        "/api/ai/templates/ticket-preview",
        headers=_headers(app, owner, tenant.slug),
        json=request_body,
    )
    assert allowed.status_code == 200
    payload = allowed.get_json()
    assert payload["rendered_text"] is None
    assert payload["resolved_variables"] == ["nro_ticket"]
    assert payload["unresolved_variables"] == ["variable_no_autorizada"]
    assert payload["readiness"] == {
        "ready_to_insert": False,
        "server_rendered": False,
        "blocker": "unresolved_variables",
    }
    assert payload["rendering_policy"]["client_interpolation_allowed"] is False


def test_ticket_preview_supports_explicit_tenant_and_pyme_source_models(client, app):
    owner, tenant = _tenant("preview-sources")
    citizen = User(
        email="ai-contract-citizen@test.com",
        name="Nombre TenantTicket",
        rol="usuario",
    )
    citizen.set_password("pass")
    db.session.add(citizen)
    db.session.flush()
    tenant_ticket = TenantTicket(
        tenant_id=tenant.id,
        user_id=citizen.id,
        categoria="Servicios",
        descripcion="Cuerpo no expuesto como variable",
        estado="nuevo",
    )
    pyme_ticket = PymeTicket(
        tenant_id=tenant.id,
        pregunta="Consulta comercial no expuesta como variable",
        categoria="Ventas",
        estado="abierto",
        nro_ticket=88001,
        datos_extra={"contact": {"name": "Nombre PymeTicket"}},
    )
    db.session.add_all([tenant_ticket, pyme_ticket])
    db.session.commit()
    template = _template(
        "Fuentes explícitas",
        "Hola {{user_name}}. Caso {{ticket_nro}}: {{status}}.",
        tenant_id=tenant.id,
    )
    headers = _headers(app, owner, tenant.slug)

    tenant_response = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": template.id,
            "ticket_id": tenant_ticket.id,
            "source_model": "TenantTicket",
        },
    )
    assert tenant_response.status_code == 200
    assert tenant_response.get_json()["rendered_text"] == (
        f"Hola Nombre TenantTicket. Caso {tenant_ticket.id}: nuevo."
    )

    pyme_response = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": template.id,
            "ticket_id": pyme_ticket.id,
            "source_model": "PymeTicket",
        },
    )
    assert pyme_response.status_code == 200
    assert pyme_response.get_json()["rendered_text"] == (
        "Hola Nombre PymeTicket. Caso 88001: abierto."
    )

    alias_is_rejected = client.post(
        "/api/ai/templates/ticket-preview",
        headers=headers,
        json={
            "template_id": template.id,
            "ticket_id": tenant_ticket.id,
            "source_model": "tenant_ticket",
        },
    )
    assert alias_is_rejected.status_code == 400
    assert alias_is_rejected.get_json()["reason_code"] == "source_model_invalid"


def test_unknown_length_oversized_json_is_rejected_before_provider_or_database(client, app):
    owner, tenant = _tenant("chunked-limit")
    template = _template(
        "Sin mutación",
        "Hola {{user_name}}.",
        tenant_id=tenant.id,
        embedding=[0.1, 0.2],
    )
    ticket = _municipio_ticket(tenant)
    headers = _headers(app, owner, tenant.slug)
    padding = "x" * 40_000
    before_count = PlantillasRespuesta.query.count()
    before_text = template.text

    requests = (
        (
            "/api/ai/suggest-templates",
            "POST",
            {"asunto": "Consulta", "contexto_ticket": "Seguro", "padding": padding},
            AI_TEMPLATE_SUGGESTIONS_CONTRACT_VERSION,
            MAX_SUGGESTION_REQUEST_BYTES,
        ),
        (
            "/api/ai/templates",
            "POST",
            {"name": "Nueva", "text": "Respuesta", "padding": padding},
            AI_TEMPLATES_CONTRACT_VERSION,
            MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
        ),
        (
            f"/api/ai/templates/{template.id}",
            "PUT",
            {"text": "Texto que no debe persistirse", "padding": padding},
            AI_TEMPLATES_CONTRACT_VERSION,
            MAX_TEMPLATE_MUTATION_REQUEST_BYTES,
        ),
        (
            "/api/ai/templates/ticket-preview",
            "POST",
            {
                "template_id": template.id,
                "ticket_id": ticket.id,
                "source_model": "MunicipioTicket",
                "padding": padding,
            },
            AI_TEMPLATE_TICKET_PREVIEW_CONTRACT_VERSION,
            MAX_PREVIEW_REQUEST_BYTES,
        ),
        (
            "/api/ai/generate-template-text",
            "POST",
            {"prompt": "Respuesta profesional", "padding": padding},
            AI_TEMPLATES_CONTRACT_VERSION,
            MAX_GENERATE_TEMPLATE_REQUEST_BYTES,
        ),
        (
            "/api/ai/improve-template-text",
            "POST",
            {"text_to_improve": "Respuesta breve", "padding": padding},
            AI_TEMPLATES_CONTRACT_VERSION,
            MAX_IMPROVE_TEMPLATE_REQUEST_BYTES,
        ),
    )

    with (
        patch("routes.ai.embed_textos_llm") as suggestion_embed,
        patch("routes.ai_templates.embed_textos_llm") as template_embed,
        patch("routes.ai_templates.llamar_llm_para_generacion_texto") as template_llm,
    ):
        results = [
            _request_without_content_length(
                app,
                path=path,
                method=method,
                headers=headers,
                payload=payload,
            )
            for path, method, payload, _contract_version, _max_bytes in requests
        ]

    responses = [response for response, _bytes_read in results]
    assert [response.status_code for response in responses] == [
        413,
        413,
        413,
        413,
        413,
        413,
    ]
    for (response, bytes_read), (*_request, contract_version, max_bytes) in zip(
        results,
        requests,
    ):
        body = response.get_json()
        assert body["contract_version"] == contract_version
        assert body["reason_code"] == "request_body_too_large"
        assert body["field"] == "body"
        assert bytes_read <= max_bytes + 1
    suggestion_embed.assert_not_called()
    template_embed.assert_not_called()
    template_llm.assert_not_called()
    db.session.expire_all()
    assert PlantillasRespuesta.query.count() == before_count
    assert db.session.get(PlantillasRespuesta, template.id).text == before_text


def test_unknown_length_stream_preserves_a_stricter_global_limit_across_double_prime(
    client,
    app,
):
    owner, tenant = _tenant("global-stream-limit")
    headers = _headers(app, owner, tenant.slug)
    previous_limit = app.config.get("MAX_CONTENT_LENGTH")
    app.config["MAX_CONTENT_LENGTH"] = 32
    try:
        with patch("routes.ai.embed_textos_llm", return_value=[[1.0, 0.0]]) as embed:
            exact_response, exact_bytes_read = _request_without_content_length(
                app,
                path="/api/ai/suggest-templates",
                method="POST",
                headers=headers,
                payload=_payload_with_exact_json_size(32),
            )
            oversized_response, oversized_bytes_read = _request_without_content_length(
                app,
                path="/api/ai/suggest-templates",
                method="POST",
                headers=headers,
                payload=_payload_with_exact_json_size(33),
            )
    finally:
        app.config["MAX_CONTENT_LENGTH"] = previous_limit

    assert exact_response.status_code == 200
    assert exact_bytes_read == 32
    assert oversized_response.status_code == 413
    assert oversized_bytes_read <= 33
    assert oversized_response.get_json()["reason_code"] == "request_body_too_large"
    embed.assert_called_once()


def test_tenant_middleware_does_not_read_unknown_length_body_for_unmatched_route(app):
    response, bytes_read = _request_without_content_length(
        app,
        path="/api/ai/definitely-not-a-route",
        method="POST",
        headers={},
        payload={"padding": "x" * 262_144},
    )

    assert response.status_code == 404
    assert bytes_read == 0


def test_legacy_generation_endpoints_use_bounded_json_without_logging_submitted_text(
    client,
    app,
    caplog,
):
    owner, tenant = _tenant("legacy-generation")
    headers = _headers(app, owner, tenant.slug)

    with patch(
        "routes.ai_templates.llamar_llm_para_generacion_texto",
        side_effect=["Respuesta generada", "Respuesta mejorada"],
    ) as provider:
        generated = client.post(
            "/api/ai/generate-template-text",
            headers=headers,
            json={"prompt": "Redactá una respuesta institucional breve"},
        )
        improved = client.post(
            "/api/ai/improve-template-text",
            headers=headers,
            json={"text_to_improve": "Texto a mejorar"},
        )

    assert generated.status_code == 200
    assert generated.get_json() == {"generated_text": "Respuesta generada"}
    assert improved.status_code == 200
    assert improved.get_json() == {"improved_text": "Respuesta mejorada"}
    assert provider.call_count == 2

    private_marker = "CONTENIDO_PRIVADO_NO_LOGUEAR"
    caplog.clear()
    with patch(
        "routes.ai_templates.llamar_llm_para_generacion_texto",
        side_effect=RuntimeError(private_marker),
    ):
        failed = client.post(
            "/api/ai/generate-template-text",
            headers=headers,
            json={"prompt": private_marker},
        )
    assert failed.status_code == 500
    assert private_marker not in caplog.text


def test_invalid_unicode_is_rejected_before_embedding(client, app):
    owner, tenant = _tenant("invalid-unicode")
    with patch("routes.ai.embed_textos_llm") as embed:
        response = client.post(
            "/api/ai/suggest-templates",
            headers=_headers(app, owner, tenant.slug),
            json={"asunto": "\ud800"},
        )

    assert response.status_code == 400
    assert response.get_json()["reason_code"] == "asunto_invalid"
    embed.assert_not_called()
