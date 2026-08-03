from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import unicodedata

import pytest

from database import db
from models import EncEncuesta, EncLink, EncPregunta, EncRespuesta, TenantProfile, User
from services.encuestas_service import _public_schedule_now
from services.survey_governance import create_release
from utils.auth_helpers import generar_token


def _tenant(slug: str) -> tuple[User, TenantProfile]:
    owner = User(
        name=f"Owner {slug}",
        email=f"{slug}@example.test",
        rol="admin",
        tipo_chat="pyme",
        tenant_slug=slug,
    )
    owner.set_password("secret123")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=f"Organización {slug}",
        tipo="pyme",
        pyme_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()
    return owner, tenant


def _headers(owner: User, tenant: TenantProfile) -> dict[str, str]:
    token = generar_token(
        owner.id,
        owner.rol,
        owner.tipo_chat,
        owner.municipio_id,
        owner.pyme_id,
        extra_claims={"tenant_id": tenant.id, "tenant_slug": tenant.slug},
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant": tenant.slug,
        "X-Tenant-Slug": tenant.slug,
    }


def _survey(
    tenant: TenantProfile,
    *,
    slug: str,
    state: str = "publicada",
    voting: bool = False,
) -> EncEncuesta:
    now = _public_schedule_now()
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo=f"Instrumento {slug}",
        descripcion="Prueba de contrato operativo",
        tipo="votacion" if voting else "opinion",
        estado=state,
        inicio_at=now - timedelta(hours=1),
        fin_at=now + timedelta(days=2),
        es_votacion_envivo=voting,
        mostrar_resultados_envivo=voting,
        politica_unicidad="libre",
        anonimo_permitido=True,
    )
    question = EncPregunta(
        encuesta=survey,
        orden=1,
        tipo="abierta",
        texto="¿Qué opinás?",
        obligatoria=False,
    )
    db.session.add_all([survey, question])
    db.session.flush()
    if state == "publicada":
        db.session.add(EncLink(encuesta_id=survey.id, slug_publico=slug, canal="web"))
    db.session.commit()
    return survey


def _response(survey: EncEncuesta, key: str) -> None:
    db.session.add(
        EncRespuesta(
            encuesta_id=survey.id,
            tenant_id=survey.tenant_id,
            huella_unica=key,
            canal="web",
            submitted_at=datetime.now(timezone.utc),
        )
    )
    db.session.commit()


def _governance_payload() -> dict:
    public_text = "Acepto participar en esta consulta institucional."
    normalized = unicodedata.normalize("NFC", public_text).strip(" \n")
    return {
        "eligibility_policy": {
            "policy_version": "eligibility-2026.1",
            "mode": "self_attested",
            "declarations": ["membership_attested"],
            "human_review_required": True,
            "automated_decision": False,
        },
        "consent_policy": {
            "policy_version": "consent-2026.1",
            "public_text": public_text,
            "text_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            "required": True,
        },
        "decision_rules": {
            "quorum": {"type": "minimum_responses", "value": 5},
            "tie": {"procedure": "human_review"},
            "challenge": {
                "enabled": True,
                "window_hours": 48,
                "procedure": "human_review",
            },
            "human_review_required": True,
            "declarative_only": True,
        },
    }


def test_admin_list_v2_reconciles_only_explicit_tenant_metrics(client, monkeypatch):
    import config.feature_flags as feature_flags
    import routes.encuestas_admin as admin_routes

    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(admin_routes, "FEATURE_ENCUESTAS", True)
    owner_a, tenant_a = _tenant("survey-ops-a")
    _, tenant_b = _tenant("survey-ops-b")
    survey_a = _survey(tenant_a, slug="vote-a", voting=True)
    survey_b = _survey(tenant_b, slug="survey-b")
    _response(survey_a, "tenant-a-response")
    _response(survey_b, "tenant-b-response-1")
    _response(survey_b, "tenant-b-response-2")

    response = client.get("/api/admin/encuestas", headers=_headers(owner_a, tenant_a))

    assert response.status_code == 200, response.get_json()
    payload = response.get_json()
    assert payload["contract_version"] == "surveys.admin_list.v2"
    assert payload["tenant"] == {"id": tenant_a.id, "slug": tenant_a.slug}
    assert payload["freshness"]["synthetic"] is False
    assert datetime.fromisoformat(payload["freshness"]["generated_at"])
    assert [item["id"] for item in payload["encuestas"]] == [survey_a.id]
    lifecycle = payload["encuestas"][0]["admin_lifecycle"]
    assert lifecycle["instrument_kind"] == "voting"
    assert lifecycle["phase"] == "live_voting"
    assert lifecycle["accepts_responses"] is True
    assert lifecycle["participation"]["responses"] == 1
    assert lifecycle["participation"]["unique_participants"] == 1
    assert lifecycle["participation"]["abstentions"] is None
    assert lifecycle["participation"]["participation_rate"] is None
    assert lifecycle["capabilities"]["can_close"] is True
    assert payload["resumen"]["total_respuestas"] == 1
    assert payload["resumen"]["respuestas_ultimas_24h"] == 1
    assert payload["resumen"]["por_tipo_instrumento"] == {"survey": 0, "voting": 1}


def test_close_requires_published_state_and_retry_is_idempotent(client, monkeypatch):
    import config.feature_flags as feature_flags
    import routes.encuestas_admin as admin_routes

    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(admin_routes, "FEATURE_ENCUESTAS", True)
    owner, tenant = _tenant("survey-close")
    draft = _survey(tenant, slug="draft-close", state="borrador")
    published = _survey(tenant, slug="published-close", state="publicada")
    headers = _headers(owner, tenant)

    rejected = client.post(f"/api/v2/surveys/{draft.id}/close", headers=headers)
    assert rejected.status_code == 409
    assert rejected.get_json()["reason_code"] == "survey_not_closable"

    first = client.post(f"/api/v2/surveys/{published.id}/close", headers=headers)
    second = client.post(f"/api/v2/surveys/{published.id}/close", headers=headers)
    assert first.status_code == 200, first.get_json()
    assert second.status_code == 200, second.get_json()
    assert first.get_json()["estado"] == "cerrada"
    assert second.get_json()["fin_at"] == first.get_json()["fin_at"]

    listed = client.get("/api/admin/encuestas", headers=headers).get_json()
    closed = next(item for item in listed["encuestas"] if item["id"] == published.id)
    assert closed["admin_lifecycle"]["phase"] == "closed"
    assert closed["admin_lifecycle"]["accepts_responses"] is False
    assert closed["admin_lifecycle"]["capabilities"]["can_publish"] is False
    assert closed["admin_lifecycle"]["capabilities"]["can_close"] is False


def test_governed_draft_disables_legacy_publish_and_close_actions(client, monkeypatch):
    import config.feature_flags as feature_flags
    import routes.encuestas_admin as admin_routes

    monkeypatch.setattr(feature_flags, "FEATURE_ENCUESTAS", True)
    monkeypatch.setattr(admin_routes, "FEATURE_ENCUESTAS", True)
    owner, tenant = _tenant("survey-governed-ops")
    survey = _survey(tenant, slug="governed-draft", state="borrador", voting=True)
    create_release(
        tenant_id=tenant.id,
        survey_id=survey.id,
        actor_user_id=owner.id,
        payload=_governance_payload(),
        idempotency_key="governed-admin-list-release-0001",
    )

    response = client.get("/api/admin/encuestas", headers=_headers(owner, tenant))

    assert response.status_code == 200, response.get_json()
    lifecycle = response.get_json()["encuestas"][0]["admin_lifecycle"]
    assert lifecycle["capabilities"]["can_publish"] is False
    assert lifecycle["capabilities"]["can_close"] is False
    assert lifecycle["actions"]["publish"]["disabled_reason_code"] == "survey_governance_release_required"
    assert lifecycle["actions"]["close"]["disabled_reason_code"] == "survey_governance_release_required"
