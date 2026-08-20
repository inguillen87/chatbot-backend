from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib

import jwt
import pytest

from database import db
from models import EncEncuesta, TenantProfile, User
from models_survey_jurisdiction import SurveyContentReceipt
from services.encuestas_service import (
    EncuestaError,
    _ensure_locked_public_encuesta,
    create_encuesta,
    delete_encuesta,
    get_public_encuesta,
    list_encuestas_page,
    list_public_encuestas_for_tenant,
    publicar_encuesta,
    update_encuesta,
)
from socket_service import _is_authorized_survey_room
from services.survey_jurisdiction import (
    SurveyJurisdictionError,
    assert_publication_allowed,
    jurisdiction_contract,
    review_survey_content,
)
from services.survey_governance import (
    SurveyGovernanceError,
    create_release,
    publish_release,
)


@pytest.fixture(autouse=True)
def _restore_jurisdiction_gate_config(client):
    """Keep canary-mode mutations from leaking into later test modules."""

    config = client.application.config
    previous_mode = config.get("SURVEY_JURISDICTION_GATE_MODE", "observe")
    previous_tenant_ids = config.get("SURVEY_JURISDICTION_GATE_TENANT_IDS", "")
    try:
        yield
    finally:
        config["SURVEY_JURISDICTION_GATE_MODE"] = previous_mode
        config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = previous_tenant_ids


def _user_and_tenant(*, verified: bool) -> tuple[User, TenantProfile]:
    user = User(
        name="Revisor institucional",
        email=f"review-{int(verified)}-{datetime.now().timestamp()}@test.local",
        rol="admin",
        tenant_slug=f"tenant-jur-{int(verified)}-{datetime.now().timestamp()}",
    )
    user.set_password("secret123")
    db.session.add(user)
    db.session.flush()
    tenant = TenantProfile(
        slug=user.tenant_slug,
        nombre="Municipalidad de Junín",
        tipo="municipio",
        pyme_id=user.id,
        plan="full",
    )
    if verified:
        tenant.jurisdiction_status = "verified"
        tenant.jurisdiction_ref = "ar:ba:junin"
        tenant.jurisdiction_evidence_ref = "registry:municipal-jurisdiction:junin"
        tenant.jurisdiction_verified_by_user_id = user.id
        tenant.jurisdiction_verified_at = datetime.now(timezone.utc)
    db.session.add(tenant)
    db.session.flush()
    user.tenant_id = tenant.id
    db.session.commit()
    return user, tenant


def _payload(title: str = "Prioridades urbanas") -> dict:
    return {
        "titulo": title,
        "descripcion": "Consulta municipal sometida a revisión humana.",
        "tipo": "opinion",
        "politica_unicidad": "libre",
        "preguntas": [
            {
                "question_ref": "priority",
                "orden": 1,
                "tipo": "opcion_unica",
                "texto": "¿Qué prioridad debería tratarse primero?",
                "obligatoria": True,
                "opciones": [
                    {
                        "option_ref": "lighting",
                        "orden": 1,
                        "texto": "Alumbrado",
                    },
                    {
                        "option_ref": "streets",
                        "orden": 2,
                        "texto": "Calles",
                    },
                ],
            }
        ],
    }


def _approve_exact(survey: EncEncuesta, user: User, tenant: TenantProfile):
    before = jurisdiction_contract(survey)
    return review_survey_content(
        tenant_id=tenant.id,
        survey_id=survey.id,
        actor_user_id=user.id,
        decision="approve",
        expected_content_sha256=before["content_sha256"],
        evidence_ref="ticket:JUR-001-review",
        idempotency_key=f"content-review-{survey.id}-approved",
    )


def _governance_policy() -> dict:
    consent_text = "Autorizo el tratamiento de mi respuesta para esta consulta."
    return {
        "eligibility_policy": {
            "policy_version": "eligibility-jur-001",
            "mode": "self_attested",
            "declarations": ["resident_attested"],
            "human_review_required": True,
            "automated_decision": False,
        },
        "consent_policy": {
            "policy_version": "consent-jur-001",
            "public_text": consent_text,
            "text_sha256": hashlib.sha256(consent_text.encode("utf-8")).hexdigest(),
            "required": True,
        },
        "decision_rules": {
            "quorum": {"type": "minimum_responses", "value": 1},
            "tie": {"procedure": "human_review"},
            "challenge": {
                "enabled": True,
                "window_hours": 24,
                "procedure": "human_review",
            },
            "human_review_required": True,
            "declarative_only": True,
        },
    }


def test_observe_preserves_legacy_unverified_publish_and_records_truth(client):
    with client.application.app_context():
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "observe"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = ""
        user, tenant = _user_and_tenant(verified=False)
        survey = create_encuesta(_payload(), user)

        contract = jurisdiction_contract(survey)
        assert contract["ready"] is False
        assert contract["reason_code"] == "survey_tenant_jurisdiction_unverified"
        assert contract["allowed_to_publish"] is True
        assert survey.content_origin == "manual"
        assert survey.jurisdiction_ref is None

        survey, link = publicar_encuesta(survey.id, user)
        assert survey.estado == "publicada"
        assert get_public_encuesta(link.slug_publico).id == survey.id
        assert [row.event_type for row in SurveyContentReceipt.query.all()] == [
            "created",
            "published",
        ]


def test_enforce_publish_requires_verified_binding_and_exact_human_review(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=False)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )
        survey = create_encuesta(_payload(), user)

        with pytest.raises(EncuestaError) as blocked:
            publicar_encuesta(survey.id, user)
        assert blocked.value.payload["reason_code"] == (
            "survey_tenant_jurisdiction_unverified"
        )
        assert db.session.get(EncEncuesta, survey.id).estado == "borrador"

        tenant.jurisdiction_status = "verified"
        tenant.jurisdiction_ref = "ar:ba:junin"
        tenant.jurisdiction_evidence_ref = "registry:municipal-jurisdiction:junin"
        tenant.jurisdiction_verified_by_user_id = user.id
        tenant.jurisdiction_verified_at = datetime.now(timezone.utc)
        db.session.commit()

        expected_before_binding = jurisdiction_contract(survey)["content_sha256"]
        receipt, replayed = review_survey_content(
            tenant_id=tenant.id,
            survey_id=survey.id,
            actor_user_id=user.id,
            decision="approve",
            expected_content_sha256=expected_before_binding,
            evidence_ref="ticket:JUR-001-review",
            idempotency_key="legacy-review-approved-001",
        )
        assert replayed is False
        assert receipt.request_content_sha256 == expected_before_binding
        assert survey.jurisdiction_ref == tenant.jurisdiction_ref
        assert jurisdiction_contract(survey)["ready"] is True

        replay, replayed = review_survey_content(
            tenant_id=tenant.id,
            survey_id=survey.id,
            actor_user_id=user.id,
            decision="approve",
            expected_content_sha256=expected_before_binding,
            evidence_ref="ticket:JUR-001-review",
            idempotency_key="legacy-review-approved-001",
        )
        assert replayed is True
        assert replay.id == receipt.id

        survey, _ = publicar_encuesta(survey.id, user)
        assert survey.estado == "publicada"


def test_any_content_update_invalidates_review_and_conflict_fails_closed(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=True)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )
        survey = create_encuesta(_payload(), user)
        _approve_exact(survey, user, tenant)
        assert jurisdiction_contract(survey)["ready"] is True

        survey = update_encuesta(
            survey.id,
            {"titulo": "Contenido cambiado después de aprobar"},
            user,
        )
        stale = jurisdiction_contract(survey)
        assert stale["ready"] is False
        assert stale["reason_code"] == "survey_content_review_required"
        with pytest.raises(SurveyJurisdictionError):
            assert_publication_allowed(survey)

        survey.jurisdiction_ref = "ar:tf:ushuaia"
        with pytest.raises(SurveyJurisdictionError) as conflict:
            assert_publication_allowed(survey)
        assert conflict.value.reason_code == "survey_jurisdiction_binding_conflict"
        db.session.rollback()


def test_visibility_rollout_hides_unreviewed_preexisting_public_content(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=False)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "observe"
        survey = create_encuesta(_payload(), user)
        survey, link = publicar_encuesta(survey.id, user)

        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = (
            "enforce_visibility"
        )
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )
        assert list_public_encuestas_for_tenant(tenant.id) == []
        assert _is_authorized_survey_room(
            f"encuesta:{tenant.slug}:{link.slug_publico}"
        ) is False
        with pytest.raises(EncuestaError) as hidden:
            get_public_encuesta(link.slug_publico)
        assert hidden.value.status_code == 404

        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "observe"
        assert get_public_encuesta(link.slug_publico).id == survey.id
        assert _is_authorized_survey_room(
            f"encuesta:{tenant.slug}:{link.slug_publico}"
        ) is True


def test_enforced_published_content_requires_duplicate_before_mutation(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=True)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )
        survey = create_encuesta(_payload("Contenido aprobado"), user)
        _approve_exact(survey, user, tenant)
        survey, link = publicar_encuesta(survey.id, user)
        survey_id = int(survey.id)
        public_slug = link.slug_publico
        tenant_id = int(tenant.id)
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            client.application.config["SECRET_KEY"],
            algorithm="HS256",
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }

    blocked = client.patch(
        f"/api/v2/surveys/{survey_id}",
        json={"titulo": "Contenido no revisado de otra jurisdicción"},
        headers=headers,
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["reason_code"] == (
            "survey_published_content_mutation_requires_duplicate"
    )

    with client.application.app_context():
        db.session.expire_all()
        stored = db.session.get(EncEncuesta, survey_id)
        assert stored.titulo == "Contenido aprobado"
        assert get_public_encuesta(public_slug).titulo == "Contenido aprobado"
        assert [item.id for item, _slug in list_public_encuestas_for_tenant(tenant_id)] == [
            survey_id
        ]
        # This is the same locked eligibility boundary used by response intake.
        _ensure_locked_public_encuesta(stored)


def test_content_receipts_make_delete_policy_explicit(client):
    with client.application.app_context():
        user, _tenant = _user_and_tenant(verified=True)
        survey = create_encuesta(_payload("Borrador auditable"), user)

        with pytest.raises(EncuestaError) as blocked:
            delete_encuesta(survey.id, user)
        assert blocked.value.payload["reason_code"] == (
            "survey_content_receipt_delete_blocked"
        )
        assert db.session.get(EncEncuesta, survey.id) is not None


def test_governance_publish_uses_the_same_jurisdiction_gate(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=True)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )

        blocked_survey = create_encuesta(_payload("Release sin revisión"), user)
        blocked_release, _ = create_release(
            tenant_id=tenant.id,
            survey_id=blocked_survey.id,
            actor_user_id=user.id,
            payload=_governance_policy(),
            idempotency_key="governance-create-blocked-001",
        )
        with pytest.raises(SurveyGovernanceError) as blocked:
            publish_release(
                tenant_id=tenant.id,
                survey_id=blocked_survey.id,
                release_id=blocked_release.id,
                actor_user_id=user.id,
                idempotency_key="governance-publish-blocked-001",
                expected_snapshot_sha256=blocked_release.snapshot_sha256,
            )
        assert blocked.value.reason_code == "survey_content_review_required"
        db.session.rollback()

        reviewed_survey = create_encuesta(_payload("Release revisado"), user)
        _approve_exact(reviewed_survey, user, tenant)
        release, _ = create_release(
            tenant_id=tenant.id,
            survey_id=reviewed_survey.id,
            actor_user_id=user.id,
            payload=_governance_policy(),
            idempotency_key="governance-create-reviewed-001",
        )
        published, replayed = publish_release(
            tenant_id=tenant.id,
            survey_id=reviewed_survey.id,
            release_id=release.id,
            actor_user_id=user.id,
            idempotency_key="governance-publish-reviewed-001",
            expected_snapshot_sha256=release.snapshot_sha256,
        )
        assert replayed is False
        assert published.status == "published"
        assert db.session.get(EncEncuesta, reviewed_survey.id).estado == "publicada"


def test_invalid_enforcement_config_fails_closed_and_admin_reads_do_not_write(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=True)
        survey = create_encuesta(_payload(), user)
        receipts_before = SurveyContentReceipt.query.count()
        surveys_before = EncEncuesta.query.count()

        contract = jurisdiction_contract(survey)
        list_encuestas_page(tenant.id)
        assert contract["content_sha256"]
        assert SurveyContentReceipt.query.count() == receipts_before
        assert EncEncuesta.query.count() == surveys_before

        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = ""
        with pytest.raises(SurveyJurisdictionError) as invalid:
            assert_publication_allowed(survey)
        assert invalid.value.reason_code == (
            "survey_jurisdiction_gate_configuration_invalid"
        )


def test_client_cannot_claim_server_owned_jurisdiction_or_origin(client):
    with client.application.app_context():
        user, _tenant = _user_and_tenant(verified=True)
        payload = _payload()
        payload["jurisdiction_ref"] = "ar:tf:ushuaia"
        payload["content_origin"] = "template_catalog"
        with pytest.raises(EncuestaError) as rejected:
            create_encuesta(payload, user)
        assert rejected.value.payload["reason_code"] == (
            "survey_jurisdiction_server_owned_fields"
        )


def test_v2_review_route_is_read_only_on_get_and_rejects_reserved_create_fields(client):
    with client.application.app_context():
        user, tenant = _user_and_tenant(verified=True)
        client.application.config["SURVEY_JURISDICTION_GATE_MODE"] = "enforce_publish"
        client.application.config["SURVEY_JURISDICTION_GATE_TENANT_IDS"] = str(
            tenant.id
        )
        survey = create_encuesta(_payload(), user)
        token = jwt.encode(
            {
                "user_id": user.id,
                "rol": user.rol,
                "tenant_slug": tenant.slug,
                "exp": datetime.now(timezone.utc) + timedelta(hours=1),
            },
            client.application.config["SECRET_KEY"],
            algorithm="HS256",
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Tenant-Slug": tenant.slug,
        }
        count_before = SurveyContentReceipt.query.count()
        survey_id = int(survey.id)

    readiness = client.get(
        f"/api/v2/surveys/{survey_id}/content-review",
        headers=headers,
    )
    assert readiness.status_code == 200, readiness.get_json()
    expected_hash = readiness.get_json()["jurisdiction"]["content_sha256"]
    with client.application.app_context():
        assert SurveyContentReceipt.query.count() == count_before

    reviewed = client.post(
        f"/api/v2/surveys/{survey_id}/content-review",
        json={
            "decision": "approve",
            "expected_content_sha256": expected_hash,
            "evidence_ref": "ticket:JUR-002-route-review",
        },
        headers={**headers, "Idempotency-Key": "route-review-approved-001"},
    )
    assert reviewed.status_code == 201, reviewed.get_json()
    assert reviewed.get_json()["jurisdiction"]["ready"] is True

    rejected = client.post(
        "/api/v2/surveys",
        json={
            **_payload("Intento de metadata reservada"),
            "jurisdiction_ref": "ar:tf:ushuaia",
        },
        headers=headers,
    )
    assert rejected.status_code == 400
    assert rejected.get_json()["reason_code"] == (
        "survey_jurisdiction_server_owned_fields"
    )
