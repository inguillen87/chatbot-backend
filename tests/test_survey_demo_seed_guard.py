from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets

import pytest

from config import validate_runtime_security
from database import db
from models import (
    EncAnchorSnapshot,
    EncComentario,
    EncEncuesta,
    EncRespuesta,
    SurveyEligibilityGrant,
    SurveyEligibilityTerminal,
    SurveyGovernanceRelease,
    SurveyResponseEffect,
    SurveyResponseReceipt,
    TenantProfile,
    User,
)
from services.encuestas_service import (
    EncuestaError,
    SURVEY_DEMO_SEED_MAX_RESPONSES,
    _demo_seed_runtime_allowed,
    _reject_demo_seed_metadata_smuggling,
    create_encuesta,
    seed_encuesta_respuestas_demo,
)


@pytest.fixture
def survey_admin(client):
    owner = User(
        email="survey-demo-guard@example.com",
        name="Survey demo guard",
        rol="admin",
        tipo_chat="municipio",
        tenant_slug="survey-demo-guard",
    )
    owner.set_password(secrets.token_urlsafe(24))
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug="survey-demo-guard",
        nombre="Survey demo guard",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id
    db.session.commit()
    return owner, tenant


def _create_survey(owner: User):
    return create_encuesta(
        {
            "titulo": "Encuesta para guard de datos sintéticos",
            "preguntas": [
                {
                    "orden": 1,
                    "tipo": "opcion_unica",
                    "texto": "¿Opción?",
                    "obligatoria": True,
                    "opciones": [
                        {"orden": 1, "texto": "Sí", "valor": "si"},
                        {"orden": 2, "texto": "No", "valor": "no"},
                    ],
                }
            ],
        },
        owner,
    )


def test_production_startup_rejects_demo_seed_capability():
    errors = validate_runtime_security(
        {
            "ENV": "production",
            "SECRET_KEY": "s" * 48,
            "ALLOW_SURVEY_DEMO_SEEDING": True,
        }
    )

    assert any("ALLOW_SURVEY_DEMO_SEEDING" in error for error in errors)


def test_service_rejects_demo_seed_when_runtime_is_production_even_if_enabled(
    client,
    monkeypatch,
):
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", True)
    monkeypatch.setitem(client.application.config, "ENV", "production")

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(999999, object(), cantidad=1)

    assert exc_info.value.status_code == 403
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seeding_unsafe_runtime"
    )


def test_explicit_testing_config_is_stable_when_parent_shell_exports_production(
    client,
    monkeypatch,
):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", True)
    monkeypatch.setitem(client.application.config, "ENV", "testing")
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", True)

    assert _demo_seed_runtime_allowed() is True


def test_demo_seed_count_has_a_hard_service_limit(client):
    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            999999,
            object(),
            cantidad=SURVEY_DEMO_SEED_MAX_RESPONSES + 1,
        )

    assert exc_info.value.status_code == 422
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seed_count_exceeds_limit"
    )
    assert (
        exc_info.value.payload["max_responses"]
        == SURVEY_DEMO_SEED_MAX_RESPONSES
    )


def test_auto_seed_over_limit_fails_before_survey_persistence(
    client,
    survey_admin,
):
    owner, _tenant = survey_admin
    before = EncEncuesta.query.count()

    with pytest.raises(EncuestaError) as exc_info:
        create_encuesta(
            {
                "titulo": "No persistir auto seed excesivo",
                "auto_seed_demo": {
                    "enabled": True,
                    "cantidad": SURVEY_DEMO_SEED_MAX_RESPONSES + 1,
                },
                "preguntas": [
                    {
                        "orden": 1,
                        "tipo": "opcion_unica",
                        "texto": "¿Opción?",
                        "obligatoria": True,
                        "opciones": [{"orden": 1, "texto": "Sí"}],
                    }
                ],
            },
            owner,
        )

    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seed_count_exceeds_limit"
    )
    after = EncEncuesta.query.count()
    assert after == before


def test_public_metadata_cannot_forge_demo_batch_markers():
    with pytest.raises(EncuestaError) as exc_info:
        _reject_demo_seed_metadata_smuggling(
            {"is_demo_seed": True, "demo_batch_id": "seed-1-1234567890"}
        )

    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seed_metadata_reserved"
    )


def test_reset_preserves_all_rows_when_a_response_is_not_a_demo_batch(
    client,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    seeded = seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=2, seed=7)
    real_response = EncRespuesta(
        encuesta_id=encuesta.id,
        tenant_id=tenant.id,
        metadata_payload={"source": "citizen"},
        submitted_at=datetime.now(timezone.utc),
    )
    db.session.add(real_response)
    db.session.commit()
    before_ids = {
        row.id for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    }

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            owner,
            cantidad=1,
            reset_data=True,
            seed=8,
        )

    assert seeded["creadas"] == 2
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_reset_unclassified_data_present"
    )
    assert exc_info.value.payload["unclassified_responses"] == 1
    after_ids = {
        row.id for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    }
    assert after_ids == before_ids


def test_reset_preserves_demo_rows_when_comments_cannot_be_classified(
    client,
    survey_admin,
):
    owner, _tenant = survey_admin
    encuesta = _create_survey(owner)
    seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=1, seed=9)
    comment = EncComentario(encuesta_id=encuesta.id, texto="Comentario real")
    db.session.add(comment)
    db.session.commit()

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            owner,
            cantidad=1,
            reset_data=True,
            seed=10,
        )

    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_reset_unclassified_data_present"
    )
    assert exc_info.value.payload["unclassified_comments"] == 1
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 1
    assert EncComentario.query.filter_by(encuesta_id=encuesta.id).count() == 1


def test_reset_blocks_every_durable_governance_dependency(
    client,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=1, seed=11)
    response = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).one()
    now = datetime.now(timezone.utc)

    release = SurveyGovernanceRelease(
        tenant_id=tenant.id,
        survey_id=encuesta.id,
        version_number=1,
        status="draft",
        contract_version="surveys.governance_release.v1",
        snapshot_json="{}",
        snapshot_sha256="a" * 64,
        policy_sha256="b" * 64,
        eligibility_policy_version="eligibility-v1",
        consent_policy_version="consent-v1",
        created_by_user_id=owner.id,
        create_idempotency_key="demo-reset-release",
        create_request_hash="c" * 64,
        created_at=now,
    )
    db.session.add(release)
    db.session.flush()

    grant = SurveyEligibilityGrant(
        tenant_id=tenant.id,
        survey_id=encuesta.id,
        release_id=release.id,
        grant_ref="seg1_" + "g" * 43,
        eligibility_policy_version=release.eligibility_policy_version,
        eligibility_mode="institution_attested",
        subject_namespace="qa",
        subject_hmac="d" * 64,
        generation=1,
        subject_key_version="v1",
        authority_namespace="qa",
        authority_adapter_version="v1",
        review_reference_hmac="e" * 64,
        review_key_version="v1",
        credential_digest="f" * 64,
        credential_key_version="v1",
        issued_by_user_id=owner.id,
        issue_idempotency_key="demo-reset-grant",
        issue_request_hash="1" * 64,
        issued_at=now,
        expires_at=now + timedelta(days=1),
    )
    db.session.add(grant)
    db.session.flush()
    db.session.add_all(
        [
            EncAnchorSnapshot(
                encuesta_id=encuesta.id,
                tenant_id=tenant.id,
                algo="sha256",
                root_hash="2" * 64,
                total_respuestas=1,
                desde_at=now - timedelta(minutes=1),
                hasta_at=now,
                anchor_status="draft",
                created_by=owner.id,
            ),
            SurveyResponseReceipt(
                tenant_id=tenant.id,
                survey_id=encuesta.id,
                response_id=response.id,
                submission_id_hash="3" * 64,
                payload_hash="4" * 64,
                canonical_version="survey-response.v1",
                instrument_revision=1,
                contract_version="surveys.response_receipt.v1",
            ),
            SurveyResponseEffect(
                tenant_id=tenant.id,
                survey_id=encuesta.id,
                response_id=response.id,
                effect_type=SurveyResponseEffect.EFFECT_ANALYTICS,
                effect_key="demo-reset-effect",
                scope_key="demo-reset-effect-scope",
                payload_json={},
                status=SurveyResponseEffect.STATUS_PENDING,
                max_attempts=1,
            ),
            SurveyEligibilityTerminal(
                tenant_id=tenant.id,
                survey_id=encuesta.id,
                release_id=release.id,
                grant_id=grant.id,
                disposition="revoked",
                actor_user_id=owner.id,
                reason_code="administrative_revocation",
                idempotency_key="demo-reset-terminal",
                response_id=None,
                eligibility_policy_version=release.eligibility_policy_version,
                submission_payload_hash=None,
                request_hash="5" * 64,
                created_at=now,
            ),
        ]
    )
    db.session.commit()

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            owner,
            cantidad=1,
            reset_data=True,
            seed=12,
        )

    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_reset_durable_history_present"
    )
    assert set(exc_info.value.payload["blockers"]) == {
        "releases",
        "anchors",
        "receipts",
        "effects",
        "eligibility_grants",
        "eligibility_terminals",
    }
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 1


def test_reset_replaces_only_explicitly_marked_demo_batches(
    client,
    survey_admin,
):
    owner, _tenant = survey_admin
    encuesta = _create_survey(owner)
    first = seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=3, seed=13)

    second = seed_encuesta_respuestas_demo(
        encuesta.id,
        owner,
        cantidad=2,
        reset_data=True,
        seed=14,
    )

    assert second["reset"] == {"respuestas": 3, "comentarios": 0, "batches": 1}
    assert second["demo_batch_id"] != first["demo_batch_id"]
    remaining = EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    assert len(remaining) == 2
    assert all(
        row.metadata_payload.get("is_demo_seed") is True
        and row.metadata_payload.get("demo_batch_id") == second["demo_batch_id"]
        for row in remaining
    )
