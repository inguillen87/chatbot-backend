from __future__ import annotations

from copy import deepcopy

import pytest

from app import create_app
from config import Config
from database import db
from models import EncEncuesta, EncRespuesta, TenantProfile, User
from models_survey_governance import SurveyGovernanceRelease
from scripts.seed_qa_preview_institutional_surveys import (
    InstitutionalSurveySeedError,
    seed_institutional_surveys,
    validate_seed_runtime,
)
from services.demo_surveys import (
    build_demo_public_survey_payload,
    build_demo_survey_chat_menu,
    build_demo_surveys_votings_contract,
)
from services.institutional_demo_surveys import (
    TARGET_TENANT_SLUG,
    TARGET_USER_EMAIL,
    load_institutional_demo_survey_manifest,
)


EXPECTED_BRANCH_ID = "br-local-contract-tests"
LEGACY_IDS = (632, 633, 634, 635)


class InstitutionalSeedTestConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"check_same_thread": False}}
    ENABLE_RUNTIME_SCHEMA_SYNC = False
    ENABLE_RUNTIME_TENANT_INIT = False
    SKIP_INIT_TENANTS = True


@pytest.fixture()
def seeded_app():
    app = create_app(InstitutionalSeedTestConfig)
    context = app.app_context()
    context.push()
    db.create_all()
    user = User(
        name="Mauricio QA",
        email=TARGET_USER_EMAIL,
        password_hash="not-used-by-seed",
        rol="admin",
        tenant_slug=TARGET_TENANT_SLUG,
        tipo_chat="municipio",
    )
    db.session.add(user)
    db.session.flush()
    tenant = TenantProfile(
        slug=TARGET_TENANT_SLUG,
        nombre="Municipalidad de Junín QA",
        tipo="municipio",
        municipio_id=user.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    for survey_id in LEGACY_IDS:
        db.session.add(
            EncEncuesta(
                id=survey_id,
                tenant_id=tenant.id,
                slug=f"legacy-unverified-{survey_id}",
                titulo=f"Legacy {survey_id}",
                descripcion="Registro legado que el seed no debe modificar.",
                tipo="opinion",
                estado="borrador",
                content_origin="legacy_unverified",
                created_by=user.id,
            )
        )
    db.session.commit()
    try:
        yield app
    finally:
        db.session.remove()
        db.drop_all()
        context.pop()


def _seed(*, apply: bool) -> dict:
    return seed_institutional_surveys(
        environment="preview",
        expected_branch_id=EXPECTED_BRANCH_ID,
        expected_tenant_slug=TARGET_TENANT_SLUG,
        target_user_email=TARGET_USER_EMAIL,
        apply=apply,
        allow_sqlite_for_tests=True,
    )


def test_manifest_is_junin_only_professional_and_has_one_real_voting_semantics():
    manifest = load_institutional_demo_survey_manifest()
    surveys = manifest["surveys"]

    assert len(surveys) == 3
    assert manifest["initial_response_policy"] == {
        "count": 0,
        "synthetic_seed_enabled": False,
        "municipal_truth": False,
        "label": "Instrumentos DEMO NO OFICIAL. Comienzan sin respuestas y no representan estadísticas oficiales.",
    }
    assert all(survey["titulo"].startswith("Junín ") for survey in surveys)
    assert all("(DEMO NO OFICIAL)" in survey["titulo"] for survey in surveys)
    assert all(survey["slug"].startswith("demo-gobierno-junin-") for survey in surveys)
    assert len({survey["slug"] for survey in surveys}) == 3
    assert [survey["tipo"] for survey in surveys] == ["votacion", "encuesta", "encuesta"]
    assert surveys[0]["es_votacion_envivo"] is True
    assert surveys[0]["mostrar_resultados_envivo"] is True
    assert all(2 <= len(question["opciones"]) <= 8 for survey in surveys for question in survey["preguntas"])


def test_junin_whatsapp_demo_uses_same_slugs_titles_and_explicit_synthetic_provenance():
    manifest = load_institutional_demo_survey_manifest()
    expected = [(item["slug"], item["titulo"]) for item in manifest["surveys"]]
    contract = build_demo_surveys_votings_contract(
        sector="gobierno",
        tenant_slug="junin",
        page=1,
        page_size=3,
    )
    observed = [(item["slug"], item["titulo"]) for item in contract["items"]]

    assert observed == expected
    for item in contract["items"]:
        assert item["institutional_demo"] is True
        assert item["official"] is False
        assert item["municipal_truth"] is False
        assert item["data_mode"] == "synthetic_demo_scenario"
        assert item["data_provenance"]["mode"] == "synthetic"
        assert item["data_provenance"]["real_responses_included"] == 0
        assert item["data_provenance"]["synthetic_responses_included"] == 100
        assert item["seed"]["real_people"] is False
        assert item["seed"]["source"] == "qa_preview_institutional_manifest"
        assert "no apto para decisiones de gobierno" in item["disclaimer"]

    first_detail = build_demo_public_survey_payload(expected[0][0])
    assert first_detail is not None
    assert first_detail["titulo"] == expected[0][1]
    assert first_detail["es_votacion_envivo"] is True

    whatsapp = build_demo_survey_chat_menu(
        sector="gobierno",
        tenant_slug="junin",
        channel="whatsapp",
        page=1,
        page_size=3,
    )
    assert [(item["slug"], item["titulo"]) for item in whatsapp["surveys"]] == expected
    assert all(title in whatsapp["message_body"] for _, title in expected)
    assert "/e/" not in whatsapp["message_body"]
    vote_actions = [
        item for item in whatsapp["options_list"]
        if str(item.get("action_id") or "").startswith("chatboc_survey_open::")
    ]
    assert len(vote_actions) == 3
    assert whatsapp["pagination"]["next_action_id"] == "mostrar_menu_encuestas::2"

    unrelated = build_demo_surveys_votings_contract(
        sector="gobierno",
        tenant_slug="municipio",
    )
    unrelated_slugs = {item["slug"] for item in unrelated["all_items"]}
    assert not unrelated_slugs.intersection(slug for slug, _ in expected)


def test_seed_dry_run_apply_and_rerun_are_atomic_zero_response_and_leave_legacy_untouched(
    seeded_app,
):
    legacy_before = {
        survey.id: (survey.slug, survey.titulo, survey.estado, survey.content_origin)
        for survey in EncEncuesta.query.filter(EncEncuesta.id.in_(LEGACY_IDS)).all()
    }
    manifest = load_institutional_demo_survey_manifest()
    slugs = [survey["slug"] for survey in manifest["surveys"]]

    dry_run = _seed(apply=False)
    assert dry_run["status"] == "dry_run"
    assert dry_run["writes_performed"] is False
    assert [item["action"] for item in dry_run["surveys"]] == ["would_create"] * 3
    assert EncEncuesta.query.filter(EncEncuesta.slug.in_(slugs)).count() == 0

    applied = _seed(apply=True)
    assert applied["status"] == "applied"
    assert applied["writes_performed"] is True
    assert applied["target"]["tenant_slug"] == "junin"
    assert applied["channel_configuration_mutated"] is False
    assert applied["publication"] == {
        "performed": False,
        "state": "draft_only",
        "governance_release_created": False,
    }
    assert [item["action"] for item in applied["surveys"]] == ["created"] * 3

    created = EncEncuesta.query.filter(EncEncuesta.slug.in_(slugs)).order_by(EncEncuesta.id).all()
    assert len(created) == 3
    assert all(item.estado == "borrador" for item in created)
    assert all(item.content_origin == "seed_demo" for item in created)
    assert all(item.respuestas.count() == 0 for item in created)
    assert all(item.id not in LEGACY_IDS for item in created)
    assert SurveyGovernanceRelease.query.count() == 0
    assert EncRespuesta.query.count() == 0

    rerun = _seed(apply=True)
    assert rerun["writes_performed"] is False
    assert [item["action"] for item in rerun["surveys"]] == ["reused"] * 3
    assert EncEncuesta.query.filter(EncEncuesta.slug.in_(slugs)).count() == 3
    legacy_after = {
        survey.id: (survey.slug, survey.titulo, survey.estado, survey.content_origin)
        for survey in EncEncuesta.query.filter(EncEncuesta.id.in_(LEGACY_IDS)).all()
    }
    assert legacy_after == legacy_before


def test_seed_fails_closed_for_production_render_branch_and_tenant_drift(seeded_app):
    neon_url = (
        "postgresql+psycopg://preview_user:redacted@"
        "ep-preview-pooler.us-east-2.aws.neon.tech/chatboc?sslmode=require"
    )
    with pytest.raises(InstitutionalSurveySeedError, match="production_runtime_forbidden"):
        validate_seed_runtime(
            environment="preview",
            database_url=neon_url,
            expected_branch_id="br-preview-safe",
            environ={"VERCEL_ENV": "production"},
        )
    with pytest.raises(InstitutionalSurveySeedError, match="render_runtime_forbidden"):
        validate_seed_runtime(
            environment="preview",
            database_url=neon_url,
            expected_branch_id="br-preview-safe",
            environ={"RENDER": "true"},
        )
    with pytest.raises(InstitutionalSurveySeedError, match="expected_neon_branch_id_invalid"):
        validate_seed_runtime(
            environment="preview",
            database_url=neon_url,
            expected_branch_id="main",
            environ={},
        )
    with pytest.raises(InstitutionalSurveySeedError, match="target_tenant_slug_mismatch"):
        seed_institutional_surveys(
            environment="preview",
            expected_branch_id=EXPECTED_BRANCH_ID,
            expected_tenant_slug="otro-tenant",
            target_user_email=TARGET_USER_EMAIL,
            apply=False,
            allow_sqlite_for_tests=True,
        )


def test_manifest_loader_returns_an_isolated_copy():
    first = load_institutional_demo_survey_manifest()
    second = load_institutional_demo_survey_manifest()
    changed = deepcopy(first)
    changed["surveys"][0]["titulo"] = "mutated"

    assert second["surveys"][0]["titulo"] != changed["surveys"][0]["titulo"]
