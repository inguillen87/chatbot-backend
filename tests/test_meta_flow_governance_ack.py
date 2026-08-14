from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import secrets

import pytest

from app import db
from models import (
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    MessageTemplateRegistry,
    ProviderSender,
    TenantProfile,
    User,
    WhatsAppFlowInteraction,
)
from services.meta_flow_data_exchange import MetaFlowActionError, MetaFlowRequestContext
from services.meta_flow_json import (
    SURVEY_GOVERNANCE_ACK_CONTRACT_VERSION,
    SURVEY_GOVERNANCE_ACK_FIELDS,
    SURVEY_PRIVACY_ACK_FIELDS,
    SURVEY_VOTE_DATA_CONTRACT,
)
from services.meta_flow_runtime import (
    SURVEY_FLOW_ID,
    MetaFlowRuntime,
    apply_whatsapp_flow_completion,
    authorize_survey_context,
    replay_whatsapp_flow_completion,
)
from services.survey_governance import create_release, publish_release
from services.whatsapp_flow_security import consume_whatsapp_flow_interaction


ACK_CONTRACT_VERSION = SURVEY_GOVERNANCE_ACK_CONTRACT_VERSION
ACK_FIELDS = list(SURVEY_GOVERNANCE_ACK_FIELDS)


def _scope(slug: str) -> tuple[User, TenantProfile, ProviderSender, str]:
    owner = User(
        email=f"{slug}@example.test",
        name=slug,
        rol="tenant_admin",
        tipo_chat="municipio",
    )
    owner.set_password(secrets.token_urlsafe(24))
    db.session.add(owner)
    db.session.flush()
    endpoint_alias = f"{slug}-endpoint"
    tenant = TenantProfile(
        slug=slug,
        nombre=slug.title(),
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
        configuracion={
            "meta_platform": {
                "data_exchange": {"endpoint_aliases": [endpoint_alias]}
            }
        },
    )
    db.session.add(tenant)
    db.session.flush()
    sender = ProviderSender(
        tenant_id=tenant.id,
        channel="whatsapp",
        sender_id=f"whatsapp:+1555{tenant.id:07d}",
        phone_number=f"+1555{tenant.id:07d}",
        waba_id=f"waba-{tenant.id}",
        status="active",
        metadata_json={"meta_flow_data_exchange_endpoint_id": endpoint_alias},
    )
    db.session.add(sender)
    db.session.commit()
    return owner, tenant, sender, endpoint_alias


def _survey(
    tenant: TenantProfile,
    slug: str,
    *,
    governed: bool,
    privacy_required: bool = False,
) -> tuple[EncEncuesta, object | None]:
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo="Prioridad ciudadana",
        estado="borrador" if governed else "publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        inicio_at=None,
        fin_at=None,
        politica_unicidad="por_cookie",
        anonimo_permitido=True,
        privacy_policy_version="privacy-meta-flow-v1" if privacy_required else None,
        privacy_policy_url=(
            "https://example.test/privacy/meta-flow-v1"
            if privacy_required
            else None
        ),
        privacy_consent_required=privacy_required,
    )
    question = EncPregunta(
        orden=1,
        tipo="opcion_unica",
        texto="Se debe priorizar esta mejora?",
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(orden=1, texto="Si"),
        EncOpcion(orden=2, texto="No"),
    ]
    survey.preguntas = [question]
    db.session.add(survey)
    db.session.commit()
    if not governed:
        return survey, None

    consent_text = "Acepto participar bajo las reglas publicadas de esta consulta."
    release, _ = create_release(
        tenant_id=tenant.id,
        survey_id=survey.id,
        actor_user_id=tenant.municipio_id,
        payload={
            "eligibility_policy": {
                "policy_version": "eligibility-meta-flow-v1",
                "mode": "self_attested",
                "declarations": ["resident_attested"],
                "human_review_required": True,
                "automated_decision": False,
            },
            "consent_policy": {
                "policy_version": "consent-meta-flow-v1",
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
        },
        idempotency_key=f"governance:create:{tenant.id}",
    )
    release, _ = publish_release(
        tenant_id=tenant.id,
        survey_id=survey.id,
        release_id=release.id,
        actor_user_id=tenant.municipio_id,
        idempotency_key=f"governance:publish:{tenant.id}",
        expected_snapshot_sha256=release.snapshot_sha256,
    )
    db.session.refresh(survey)
    return survey, release


def _interaction(
    tenant: TenantProfile,
    sender: ProviderSender,
    survey: EncEncuesta,
    *,
    data_contract: list[str],
    staged: bool,
) -> WhatsAppFlowInteraction:
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"governance-flow-{tenant.id}",
        language="es",
        status="approved",
        content_sid=f"HXGOVERNANCE{tenant.id}",
        external_template_id=f"meta-governance-{tenant.id}",
    )
    db.session.add(registry)
    db.session.flush()
    metadata = {
        "survey_context": {
            "id": str(survey.id),
            "slug": survey.slug,
            "instrument_revision": int(survey.structure_revision or 1),
        }
    }
    if staged:
        metadata["survey_staged_answers"] = {
            str(survey.preguntas[0].id): survey.preguntas[0].opciones[0].id
        }
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=SURVEY_FLOW_ID,
        meta_flow_id=registry.external_template_id,
        content_sid=registry.content_sid,
        recipient_hash=f"{tenant.id:064x}"[-64:],
        recipient_hint="***1234",
        token_digest=f"{tenant.id + 900:064x}"[-64:],
        idempotency_key=f"governance-flow-send-{tenant.id}",
        status="sent",
        data_contract=data_contract,
        metadata_json=metadata,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.session.add(interaction)
    db.session.commit()
    return interaction


def _runtime(
    tenant: TenantProfile,
    sender: ProviderSender,
    endpoint_alias: str,
    interaction: WhatsAppFlowInteraction,
) -> tuple[MetaFlowRuntime, object]:
    data_contract = list(interaction.data_contract or [])

    def verifier(_token, **_scope):
        return {
            "interaction_id": interaction.id,
            "tenant_id": tenant.id,
            "provider_sender_id": sender.id,
            "flow_id": SURVEY_FLOW_ID,
            "meta_flow_id": interaction.meta_flow_id,
            "recipient_hash": interaction.recipient_hash,
            "data_contract": data_contract,
        }

    prefix = f"META_FLOW_WABA_{sender.waba_id.replace('-', '_').upper()}"
    runtime = MetaFlowRuntime(
        environ={
            f"{prefix}_PRIVATE_KEY_PEM": (
                "-----BEGIN PRIVATE KEY-----\\ntest-private-key-material\\n"
                "-----END PRIVATE KEY-----"
            ),
            f"{prefix}_APP_SECRET": "app-secret-value",
            f"{prefix}_FLOW_TOKEN_KEY_V1": (
                "token-key-value-with-more-than-thirty-two-bytes"
            ),
        },
        token_verifier=verifier,
    )
    config = runtime.resolve(endpoint_alias)
    assert config is not None
    return runtime, config


def _request_context(config, action: str) -> MetaFlowRequestContext:
    return MetaFlowRequestContext(
        request_id="governance-runtime-test",
        endpoint_id=config.endpoint_id,
        tenant_id=config.tenant_id,
        waba_id=config.waba_id,
        action=action,
    )


def _init_payload() -> dict:
    return {
        "version": "3.0",
        "action": "INIT",
        "flow_token": "signed-flow-token",
        "data": {},
    }


def _answers(release) -> dict:
    return {
        "confirm_vote": True,
        "governance_ack_contract_version": ACK_CONTRACT_VERSION,
        "governance_release_id": str(release.id),
        "governance_snapshot_sha256": release.snapshot_sha256,
        "governance_eligibility_policy_version": release.eligibility_policy_version,
        "governance_consent_policy_version": release.consent_policy_version,
        "governance_consent_accepted": True,
        "governance_eligibility_acknowledged": True,
    }


def _submission(interaction: WhatsAppFlowInteraction, answers: dict) -> dict:
    return {
        "flow": {"id": SURVEY_FLOW_ID, "meta_id": interaction.meta_flow_id},
        "payload": {"answers": answers},
        "correlation": {"interaction_id": interaction.id},
    }


@pytest.mark.parametrize("data_contract", [None, []])
def test_governed_flow_authorization_rejects_absent_ack_contract(
    client,
    data_contract,
):
    _, tenant, _, _ = _scope("governance-absent-contract")
    survey, _ = _survey(tenant, "governance-absent-contract-vote", governed=True)

    with pytest.raises(MetaFlowActionError) as blocked:
        authorize_survey_context(
            tenant.id,
            {"id": str(survey.id), "slug": survey.slug},
            flow_data_contract=data_contract,
        )

    assert blocked.value.code == "survey_governance_flow_ack_contract_missing"
    assert blocked.value.status_code == 409


def test_governed_flow_authorization_accepts_complete_ack_contract(client):
    _, tenant, _, _ = _scope("governance-complete-contract")
    survey, _ = _survey(
        tenant,
        "governance-complete-contract-vote",
        governed=True,
        privacy_required=True,
    )

    context = authorize_survey_context(
        tenant.id,
        {"id": str(survey.id), "slug": survey.slug},
        flow_data_contract=list(SURVEY_VOTE_DATA_CONTRACT),
    )

    assert context == {
        "id": str(survey.id),
        "slug": survey.slug,
        "instrument_revision": 1,
    }


def test_governed_flow_rejects_missing_ack_contract_before_first_question(client):
    _, tenant, sender, endpoint_alias = _scope("governance-early-reject")
    survey, _ = _survey(tenant, "governance-early-reject-vote", governed=True)
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=["confirm_vote"],
        staged=False,
    )
    _, config = _runtime(tenant, sender, endpoint_alias, interaction)

    with pytest.raises(MetaFlowActionError) as blocked:
        config.handlers["init"](
            _init_payload(),
            _request_context(config, "init"),
        )

    assert blocked.value.code == "survey_governance_flow_ack_contract_missing"
    assert blocked.value.status_code == 409
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0
    db.session.refresh(interaction)
    assert "survey_staged_answers" not in (interaction.metadata_json or {})


def test_governed_flow_hydrates_explicit_ack_screen_from_active_release(client):
    _, tenant, sender, endpoint_alias = _scope("governance-ack-screen")
    survey, release = _survey(
        tenant,
        "governance-ack-screen-vote",
        governed=True,
        privacy_required=True,
    )
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=list(SURVEY_VOTE_DATA_CONTRACT),
        staged=False,
    )
    _, config = _runtime(tenant, sender, endpoint_alias, interaction)

    initial = config.handlers["init"](
        _init_payload(),
        _request_context(config, "init"),
    )
    assert initial["screen"] == "SURVEY_QUESTION_ONE"

    confirmation = config.handlers["data_exchange"](
        {
            "version": "3.0",
            "action": "data_exchange",
            "screen": "SURVEY_QUESTION_ONE",
            "flow_token": "signed-flow-token",
            "data": {
                "selected_option": str(survey.preguntas[0].opciones[0].id),
            },
        },
        _request_context(config, "data_exchange"),
    )

    assert confirmation["screen"] == "SURVEY_CONFIRM"
    screen_data = confirmation["data"]
    assert screen_data["governance_required"] is True
    assert screen_data["privacy_required"] is True
    assert screen_data["governance_ack_contract_version"] == ACK_CONTRACT_VERSION
    assert screen_data["governance_release_id"] == str(release.id)
    assert screen_data["governance_snapshot_sha256"] == release.snapshot_sha256
    assert screen_data["governance_eligibility_policy_version"] == (
        release.eligibility_policy_version
    )
    assert screen_data["governance_consent_policy_version"] == (
        release.consent_policy_version
    )
    assert screen_data["governance_consent_text"].startswith("Acepto participar")
    assert screen_data["governance_eligibility_statement"] == "resident_attested"
    assert screen_data["privacy_policy_version"] == survey.privacy_policy_version
    assert screen_data["privacy_policy_url"] == survey.privacy_policy_url
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_governed_flow_persists_only_explicit_verified_ack_and_replays_once(client):
    _, tenant, sender, _ = _scope("governance-accepted")
    survey, release = _survey(tenant, "governance-accepted-vote", governed=True)
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=ACK_FIELDS,
        staged=True,
    )
    answers = _answers(release)
    submission = _submission(interaction, answers)

    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-GOVERNANCE-ACCEPTED",
        commit=False,
    ) is True
    response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=submission,
        anon_id="+5491112345678",
    )
    db.session.commit()

    saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
    assert saved.governance_release_id == release.id
    assert saved.governance_eligibility_policy_version == release.eligibility_policy_version
    assert saved.governance_consent_policy_version == release.consent_policy_version
    assert saved.governance_acknowledged_at is not None
    assert response["entity"] == {"kind": "survey_response", "id": saved.id}

    replay = replay_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=submission,
    )
    assert replay["entity"] == response["entity"]
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 1


def test_governed_flow_release_pin_mismatch_fails_closed(client):
    _, tenant, sender, _ = _scope("governance-mismatch")
    survey, release = _survey(tenant, "governance-mismatch-vote", governed=True)
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=ACK_FIELDS,
        staged=True,
    )
    answers = _answers(release)
    answers["governance_snapshot_sha256"] = "0" * 64

    with pytest.raises(MetaFlowActionError) as blocked:
        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction, answers),
            anon_id="+5491112345678",
        )
    db.session.rollback()

    assert blocked.value.code == "survey_governance_ack_mismatch"
    assert blocked.value.status_code == 409
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_confirm_vote_never_counts_as_explicit_privacy_consent(client):
    _, tenant, sender, _ = _scope("governance-privacy")
    survey, release = _survey(
        tenant,
        "governance-privacy-vote",
        governed=True,
        privacy_required=True,
    )
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=[*ACK_FIELDS, *SURVEY_PRIVACY_ACK_FIELDS],
        staged=True,
    )
    answers = _answers(release)
    answers["privacy_policy_version"] = survey.privacy_policy_version

    with pytest.raises(MetaFlowActionError) as blocked:
        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction, answers),
            anon_id="+5491112345678",
        )
    db.session.rollback()

    assert blocked.value.code == "survey_privacy_consent_required"
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0


def test_non_governed_quick_vote_keeps_confirm_vote_contract(client):
    _, tenant, sender, _ = _scope("legacy-quick-vote")
    survey, _ = _survey(tenant, "legacy-quick-vote", governed=False)
    interaction = _interaction(
        tenant,
        sender,
        survey,
        data_contract=["confirm_vote"],
        staged=True,
    )

    response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=_submission(interaction, {"confirm_vote": True}),
        anon_id="+5491112345678",
    )
    db.session.commit()

    saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
    assert saved.governance_release_id is None
    assert response["entity"] == {"kind": "survey_response", "id": saved.id}
