from __future__ import annotations

from contextlib import contextmanager

from flask import current_app
from sqlalchemy import event

from database import db
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    SurveyGovernanceRelease,
    TenantProfile,
    User,
)
from services import encuestas_analytics_service as survey_analytics
from services import survey_governance
from services.encuestas_service import (
    build_admin_list_payload,
    list_encuestas_page,
)
from utils.auth_helpers import generar_token


@contextmanager
def _captured_selects():
    statements: list[str] = []

    def _capture(_conn, _cursor, statement, _parameters, _context, _many):
        normalized = " ".join(str(statement).lower().split())
        if normalized.startswith("select"):
            statements.append(normalized)

    event.listen(db.engine, "before_cursor_execute", _capture)
    try:
        yield statements
    finally:
        event.remove(db.engine, "before_cursor_execute", _capture)


def _tenant_and_headers(slug: str = "survey-list-scale"):
    owner = User(
        name=f"Owner {slug}",
        email=f"owner-{slug}@test.com",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug=slug,
    )
    owner.set_password("safe-password")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Tenant {slug}",
        tipo="municipio",
        plan="enterprise",
        municipio_id=owner.id,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()
    token = generar_token(
        owner.id,
        owner.rol,
        owner.tipo_chat,
        municipio_id=owner.id,
        pyme_id=None,
    )
    return tenant, owner, {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant.slug,
    }


def _add_surveys(
    tenant: TenantProfile,
    owner: User,
    *,
    count: int,
    questions: int = 1,
    options: int = 2,
    governed: bool = False,
    published: bool = False,
    live_results: bool = False,
) -> list[EncEncuesta]:
    surveys: list[EncEncuesta] = []
    base = EncEncuesta.query.filter_by(tenant_id=tenant.id).count()
    for index in range(base, base + count):
        survey = EncEncuesta(
            tenant_id=tenant.id,
            slug=f"{tenant.slug}-instrument-{index:04d}",
            titulo=f"Instrumento {index:04d}",
            descripcion="Resumen institucional acotado",
            tipo="votacion" if index % 2 else "opinion",
            estado="publicada" if published else "borrador",
            mostrar_resultados_envivo=live_results,
            created_by=owner.id,
        )
        for question_index in range(questions):
            question = EncPregunta(
                encuesta=survey,
                orden=question_index + 1,
                tipo="opcion_unica",
                texto=f"Pregunta {question_index + 1}",
                obligatoria=True,
            )
            for option_index in range(options):
                EncOpcion(
                    pregunta=question,
                    orden=option_index + 1,
                    texto=f"Opción {option_index + 1}",
                    valor=f"option-{option_index + 1}",
                )
        db.session.add(survey)
        db.session.flush()
        if governed:
            db.session.add(
                SurveyGovernanceRelease(
                    tenant_id=tenant.id,
                    survey_id=survey.id,
                    version_number=1,
                    status="draft",
                    snapshot_json="{}",
                    snapshot_sha256="0" * 64,
                    policy_sha256="1" * 64,
                    eligibility_policy_version="eligibility-v1",
                    consent_policy_version="consent-v1",
                    created_by_user_id=owner.id,
                    create_idempotency_key=f"create-{survey.id}",
                    create_request_hash="2" * 64,
                )
            )
        surveys.append(survey)
    db.session.commit()
    return surveys


def test_v2_list_has_hard_page_cap_cursor_and_legacy_array_compatibility(client):
    tenant, owner, headers = _tenant_and_headers("survey-pagination")
    _add_surveys(tenant, owner, count=105)

    first = client.get("/api/v2/surveys?limit=500", headers=headers)
    assert first.status_code == 200, first.get_json()
    payload = first.get_json()
    assert payload["contract_version"] == "surveys.list.v2"
    assert payload["limit"] == 100
    assert payload["total"] == 105
    assert len(payload["items"]) == 100
    assert payload["has_more"] is True
    assert payload["next_cursor"]
    assert all(item["summary_only"] is True for item in payload["items"])
    assert all("texto" not in question for item in payload["items"] for question in item["preguntas"])

    second = client.get(
        "/api/v2/surveys",
        query_string={"limit": 100, "cursor": payload["next_cursor"]},
        headers=headers,
    )
    assert second.status_code == 200, second.get_json()
    second_payload = second.get_json()
    assert len(second_payload["items"]) == 5
    assert second_payload["has_more"] is False
    assert {
        item["id"] for item in payload["items"]
    }.isdisjoint({item["id"] for item in second_payload["items"]})

    page_two = client.get(
        "/api/v2/surveys?page=2&limit=20",
        headers=headers,
    )
    assert page_two.status_code == 200
    assert len(page_two.get_json()["items"]) == 20
    assert page_two.get_json()["pagination"]["next_page"] == 3

    legacy = client.get(
        "/api/admin/encuestas?legacy=1&limit=3",
        headers=headers,
    )
    assert legacy.status_code == 200, legacy.get_json()
    assert isinstance(legacy.get_json(), list)
    assert len(legacy.get_json()) == 3
    assert legacy.headers["X-Total-Count"] == "105"
    assert legacy.headers.get("X-Next-Cursor")

    invalid = client.get("/api/v2/surveys?cursor=not-a-cursor", headers=headers)
    assert invalid.status_code == 400
    assert invalid.get_json()["reason_code"] == "survey_list_cursor_invalid"

    malformed_base64 = client.get("/api/v2/surveys?cursor=A", headers=headers)
    assert malformed_base64.status_code == 400
    assert malformed_base64.get_json()["reason_code"] == "survey_list_cursor_invalid"


def test_list_query_count_is_constant_and_governance_integrity_is_detail_only(
    client,
    monkeypatch,
):
    tenant, owner, _headers = _tenant_and_headers("survey-query-count")
    _add_surveys(
        tenant,
        owner,
        count=24,
        questions=3,
        options=4,
        governed=True,
    )

    def _deep_contract_must_not_run(*_args, **_kwargs):
        raise AssertionError("admin list invoked deep governance validation")

    monkeypatch.setattr(
        survey_governance,
        "survey_governance_contract",
        _deep_contract_must_not_run,
    )

    # Warm configuration/catalog caches so both measured pages exercise the
    # same DB-only path.
    warm = list_encuestas_page(tenant.id, limit=2)
    build_admin_list_payload(
        warm["items"],
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        pagination=warm["pagination"],
    )

    db.session.expunge_all()
    with _captured_selects() as one_page_queries:
        one = list_encuestas_page(tenant.id, limit=1)
        one_payload = build_admin_list_payload(
            one["items"],
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            pagination=one["pagination"],
        )

    db.session.expunge_all()
    with _captured_selects() as many_page_queries:
        many = list_encuestas_page(tenant.id, limit=20)
        many_payload = build_admin_list_payload(
            many["items"],
            tenant_id=tenant.id,
            tenant_slug=tenant.slug,
            pagination=many["pagination"],
        )

    assert len(one_payload["encuestas"]) == 1
    assert len(many_payload["encuestas"]) == 20
    assert len(many_page_queries) == len(one_page_queries)
    assert len(many_page_queries) <= 20
    assert sum(" from enc_pregunta " in query for query in many_page_queries) == 1
    assert sum(" from enc_opcion " in query for query in many_page_queries) == 1
    assert sum(" from survey_governance_release " in query for query in many_page_queries) == 1
    assert all(item["governance"]["summary_only"] is True for item in many_payload["encuestas"])


def test_detail_remains_deep_while_oversized_create_writes_nothing(client, monkeypatch):
    tenant, owner, headers = _tenant_and_headers("survey-create-limits")
    existing = _add_surveys(tenant, owner, count=1, questions=2, options=3)[0]

    detail = client.get(f"/api/v2/surveys/{existing.id}", headers=headers)
    assert detail.status_code == 200, detail.get_json()
    detail_payload = detail.get_json()
    assert "summary_only" not in detail_payload
    assert detail_payload["preguntas"][0]["texto"] == "Pregunta 1"
    assert len(detail_payload["preguntas"][0]["opciones"]) == 3

    monkeypatch.setitem(current_app.config, "SURVEY_INSTRUMENT_MAX_QUESTIONS", 2)
    before = EncEncuesta.query.filter_by(tenant_id=tenant.id).count()
    oversized = client.post(
        "/api/v2/surveys",
        headers=headers,
        json={
            "title": "Instrumento fuera de límite",
            "questions": [
                {
                    "type": "single",
                    "label": f"Pregunta {index}",
                    "options": ["Sí", "No"],
                }
                for index in range(3)
            ],
        },
    )
    assert oversized.status_code == 413, oversized.get_json()
    assert oversized.get_json()["reason_code"] == "survey_instrument_too_large"
    assert oversized.get_json()["field"] == "preguntas"
    assert EncEncuesta.query.filter_by(tenant_id=tenant.id).count() == before
    assert (
        EncEncuesta.query.filter_by(titulo="Instrumento fuera de límite").first()
        is None
    )

    monkeypatch.setitem(current_app.config, "SURVEY_TENANT_MAX_INSTRUMENTS", 10)
    _add_surveys(tenant, owner, count=9)
    at_quota = EncEncuesta.query.filter_by(tenant_id=tenant.id).count()
    quota_response = client.post(
        "/api/v2/surveys",
        headers=headers,
        json={
            "title": "Instrumento sobre cuota",
            "questions": [
                {"type": "single", "label": "Pregunta", "options": ["Sí", "No"]}
            ],
        },
    )
    assert quota_response.status_code == 429, quota_response.get_json()
    assert quota_response.get_json()["reason_code"] == "survey_tenant_quota_exceeded"
    assert EncEncuesta.query.filter_by(tenant_id=tenant.id).count() == at_quota


def test_live_cache_hit_question_loading_is_constant_for_large_instruments(client):
    tenant, owner, _headers = _tenant_and_headers("survey-live-cache")
    small = _add_surveys(
        tenant,
        owner,
        count=1,
        questions=1,
        options=2,
        published=True,
        live_results=True,
    )[0]
    large = _add_surveys(
        tenant,
        owner,
        count=1,
        questions=30,
        options=5,
        published=True,
        live_results=True,
    )[0]

    with survey_analytics._PUBLIC_LIVE_RESULTS_CACHE_LOCK:
        survey_analytics._PUBLIC_LIVE_RESULTS_CACHE.clear()
    survey_analytics.calculate_live_results(small.slug, include_heatmap=False)
    survey_analytics.calculate_live_results(large.slug, include_heatmap=False)

    db.session.expunge_all()
    with _captured_selects() as small_hit_queries:
        small_payload = survey_analytics.calculate_live_results(
            small.slug,
            include_heatmap=False,
        )
    db.session.expunge_all()
    with _captured_selects() as large_hit_queries:
        large_payload = survey_analytics.calculate_live_results(
            large.slug,
            include_heatmap=False,
        )

    assert small_payload["cache_etag"]
    assert large_payload["cache_etag"]
    assert len(large_hit_queries) == len(small_hit_queries)
    assert sum(" from enc_pregunta " in query for query in large_hit_queries) == 1
    assert sum(" from enc_opcion " in query for query in large_hit_queries) == 1
