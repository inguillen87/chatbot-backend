from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets

import pytest
from flask import g

from config import validate_runtime_security
from database import db
from models import (
    EncAnchorSnapshot,
    EncComentario,
    EncEncuesta,
    EncRespuesta,
    PointsTransaction,
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


def test_production_startup_requires_explicit_synthetic_seed_canaries():
    missing = validate_runtime_security(
        {
            "ENV": "production",
            "SECRET_KEY": "s" * 48,
            "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1": True,
            "SURVEY_SYNTHETIC_SEED_TENANT_IDS": "",
        }
    )
    malformed = validate_runtime_security(
        {
            "ENV": "production",
            "SECRET_KEY": "s" * 48,
            "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1": True,
            "SURVEY_SYNTHETIC_SEED_TENANT_IDS": "7,01",
        }
    )
    valid = validate_runtime_security(
        {
            "ENV": "production",
            "SECRET_KEY": "s" * 48,
            "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1": True,
            "SURVEY_SYNTHETIC_SEED_TENANT_IDS": "7,23",
        }
    )

    assert any("tenants canarios explicitos" in error for error in missing)
    assert any("SURVEY_SYNTHETIC_SEED_TENANT_IDS" in error for error in malformed)
    assert not any("SYNTHETIC_SEED" in error for error in valid)


def test_service_rejects_demo_seed_when_runtime_is_production_even_if_enabled(
    client,
    monkeypatch,
    survey_admin,
):
    owner, _tenant = survey_admin
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", True)
    monkeypatch.setitem(client.application.config, "ENV", "production")

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(999999, owner, cantidad=1)

    assert exc_info.value.status_code == 403
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seeding_unsafe_runtime"
    )


def test_explicit_testing_config_is_stable_when_parent_shell_exports_production(
    client,
    monkeypatch,
    survey_admin,
):
    owner, tenant = survey_admin
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", True)
    monkeypatch.setitem(client.application.config, "ENV", "testing")
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", True)

    assert _demo_seed_runtime_allowed(user=owner, tenant_id=tenant.id) is True


def test_demo_seed_count_has_a_hard_service_limit(client, survey_admin):
    owner, _tenant = survey_admin
    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            999999,
            owner,
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


def test_production_canary_allows_only_the_survey_tenant(
    client,
    monkeypatch,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    encuesta.puntos_recompensa = 50
    db.session.commit()
    emitted_updates = []
    monkeypatch.setattr(
        "services.encuestas_service.emit_survey_update",
        lambda *args, **kwargs: emitted_updates.append((args, kwargs)),
    )
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", False)
    monkeypatch.setitem(client.application.config, "ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", False)
    monkeypatch.setitem(
        client.application.config,
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1",
        True,
    )
    monkeypatch.setitem(
        client.application.config,
        "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
        str(tenant.id),
    )

    result = seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=2, seed=29)

    assert result["creadas"] == 2
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 2
    assert {
        row.response_origin
        for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    } == {"synthetic_demo"}
    assert PointsTransaction.query.filter_by(tenant_id=tenant.id).count() == 0
    assert SurveyResponseEffect.query.filter_by(survey_id=encuesta.id).count() == 0
    assert emitted_updates == []
    assert db.session.get(User, owner.id).saldo_puntos in {None, 0}


def test_production_canary_rejects_non_allowlisted_tenant_without_side_effects(
    client,
    monkeypatch,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", False)
    monkeypatch.setitem(client.application.config, "ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", False)
    monkeypatch.setitem(
        client.application.config,
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1",
        True,
    )
    monkeypatch.setitem(
        client.application.config,
        "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
        str(tenant.id + 1000),
    )

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=2, seed=31)

    assert exc_info.value.status_code == 403
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_synthetic_seeding_tenant_not_allowlisted"
    )
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0


def test_authorized_superadmin_can_seed_an_allowlisted_target_tenant(
    client,
    monkeypatch,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    platform_admin = User(
        email="survey-seed-platform-admin@example.com",
        name="Survey seed platform admin",
        rol="super_admin",
        tipo_chat="municipio",
    )
    platform_admin.set_password("survey-platform-test-only")
    db.session.add(platform_admin)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform_admin.email)
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", False)
    monkeypatch.setitem(client.application.config, "ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", False)
    monkeypatch.setitem(
        client.application.config,
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1",
        True,
    )
    monkeypatch.setitem(
        client.application.config,
        "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
        str(tenant.id),
    )
    g.tenant_profile = tenant

    result = seed_encuesta_respuestas_demo(
        encuesta.id,
        platform_admin,
        cantidad=2,
        seed=37,
    )

    assert result["creadas"] == 2
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 2


def test_authorized_superadmin_cannot_seed_a_non_allowlisted_target_tenant(
    client,
    monkeypatch,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    platform_admin = User(
        email="survey-seed-platform-admin-denied@example.com",
        name="Survey seed denied platform admin",
        rol="super_admin",
        tipo_chat="municipio",
    )
    platform_admin.set_password("survey-platform-denied-test-only")
    db.session.add(platform_admin)
    db.session.commit()
    monkeypatch.setenv("CLERK_SUPERADMIN_EMAILS", platform_admin.email)
    monkeypatch.setitem(client.application.config, "ALLOW_SURVEY_DEMO_SEEDING", False)
    monkeypatch.setitem(client.application.config, "ENV", "production")
    monkeypatch.setitem(client.application.config, "TESTING", False)
    monkeypatch.setitem(
        client.application.config,
        "ENABLE_SURVEY_SYNTHETIC_SEEDING_V1",
        True,
    )
    monkeypatch.setitem(
        client.application.config,
        "SURVEY_SYNTHETIC_SEED_TENANT_IDS",
        str(tenant.id + 1000),
    )
    g.tenant_profile = tenant

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            platform_admin,
            cantidad=2,
            seed=41,
        )

    assert exc_info.value.status_code == 403
    assert (
        exc_info.value.payload["reason_code"]
        == "survey_synthetic_seeding_tenant_not_allowlisted"
    )
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0


def test_qa_seed_still_requires_an_administrative_actor(
    client,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    employee = User(
        email="survey-seed-employee@example.com",
        name="Survey seed employee",
        rol="empleado",
        tipo_chat="municipio",
        tenant_id=tenant.id,
    )
    employee.set_password("survey-employee-test-only")
    db.session.add(employee)
    db.session.commit()

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(encuesta.id, employee, cantidad=1)

    assert exc_info.value.status_code == 403
    assert exc_info.value.payload["reason_code"] == "survey_demo_seeding_admin_required"
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0


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
        == "survey_demo_seed_requires_exclusive_draft"
    )
    assert exc_info.value.payload["blockers"]["real_responses"] == 1
    after_ids = {
        row.id for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    }
    assert after_ids == before_ids


def test_reset_rejects_incomplete_seed_metadata_without_contract(
    client,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    legacy_marker = EncRespuesta(
        encuesta_id=encuesta.id,
        tenant_id=tenant.id,
        response_origin="legacy_unverified",
        metadata_payload={
            "is_demo_seed": True,
            "demo_batch_id": f"seed-{encuesta.id}-1755680400000-abcdef123456",
        },
        submitted_at=datetime.now(timezone.utc),
    )
    db.session.add(legacy_marker)
    db.session.commit()

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(
            encuesta.id,
            owner,
            cantidad=1,
            reset_data=True,
            seed=81,
        )

    assert (
        exc_info.value.payload["reason_code"]
        == "survey_demo_seed_requires_exclusive_draft"
    )
    assert exc_info.value.payload["blockers"]["legacy_unverified_responses"] == 1
    assert db.session.get(EncRespuesta, legacy_marker.id) is not None


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
        == "survey_demo_seed_requires_exclusive_draft"
    )
    assert exc_info.value.payload["blockers"]["comments"] == 1
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
        == "survey_demo_seed_requires_exclusive_draft"
    )
    assert set(exc_info.value.payload["blockers"]) == {
        "governance_releases",
        "anchors",
        "response_receipts",
        "response_effects",
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
        row.response_origin == "synthetic_demo"
        and row.metadata_payload.get("is_demo_seed") is True
        and row.metadata_payload.get("demo_batch_id") == second["demo_batch_id"]
        for row in remaining
    )


def test_seed_same_request_replays_without_duplicate_side_effects(
    client,
    survey_admin,
):
    owner, _tenant = survey_admin
    encuesta = _create_survey(owner)

    first = seed_encuesta_respuestas_demo(
        encuesta.id,
        owner,
        cantidad=3,
        seed=77,
        scenario="balanced",
    )
    second = seed_encuesta_respuestas_demo(
        encuesta.id,
        owner,
        cantidad=3,
        seed=77,
        scenario="balanced",
    )

    assert first["creadas"] == 3
    assert first["reutilizadas"] == 0
    assert second["creadas"] == 0
    assert second["reutilizadas"] == 3
    assert second["demo_batch_id"] == first["demo_batch_id"]
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 3


def test_seed_rejects_real_or_unverified_rows_before_synthetic_writes(
    client,
    survey_admin,
):
    owner, tenant = survey_admin
    encuesta = _create_survey(owner)
    db.session.add_all(
        [
            EncRespuesta(
                encuesta_id=encuesta.id,
                tenant_id=tenant.id,
                response_origin="real",
                submitted_at=datetime.now(timezone.utc),
            ),
            EncRespuesta(
                encuesta_id=encuesta.id,
                tenant_id=tenant.id,
                response_origin="legacy_unverified",
                submitted_at=datetime.now(timezone.utc),
            ),
        ]
    )
    db.session.commit()
    before = {
        row.id: row.response_origin
        for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    }

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=2, seed=78)

    assert exc_info.value.payload["reason_code"] == (
        "survey_demo_seed_requires_exclusive_draft"
    )
    assert exc_info.value.payload["blockers"]["real_responses"] == 1
    assert (
        exc_info.value.payload["blockers"]["legacy_unverified_responses"]
        == 1
    )
    after = {
        row.id: row.response_origin
        for row in EncRespuesta.query.filter_by(encuesta_id=encuesta.id).all()
    }
    assert after == before


def test_seed_batch_rolls_back_every_row_when_one_persist_fails(
    client,
    monkeypatch,
    survey_admin,
):
    owner, _tenant = survey_admin
    encuesta = _create_survey(owner)
    from services import encuestas_service

    original = encuestas_service._persist_respuesta_entity
    calls = 0

    def fail_second(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise EncuestaError(
                "forced atomic failure",
                payload={"reason_code": "forced_atomic_failure"},
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(
        encuestas_service,
        "_persist_respuesta_entity",
        fail_second,
    )

    with pytest.raises(EncuestaError) as exc_info:
        seed_encuesta_respuestas_demo(encuesta.id, owner, cantidad=3, seed=79)

    assert exc_info.value.payload["reason_code"] == "forced_atomic_failure"
    assert EncRespuesta.query.filter_by(encuesta_id=encuesta.id).count() == 0
