from __future__ import annotations

from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

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
from services.meta_flow_data_exchange import MetaFlowActionError
from services.meta_flow_runtime import (
    SURVEY_FLOW_ID,
    apply_whatsapp_flow_completion,
    authorize_survey_context,
    replay_whatsapp_flow_completion,
)
from services.whatsapp_flow_security import consume_whatsapp_flow_interaction


def _tenant_scope() -> tuple[TenantProfile, ProviderSender]:
    suffix = uuid4().hex[:12]
    owner = User(
        email=f"survey-demographics-{suffix}@example.test",
        name="Survey demographics owner",
        rol="tenant_admin",
        tipo_chat="municipio",
    )
    owner.set_password(secrets.token_urlsafe(24))
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=f"survey-demographics-{suffix}",
        nombre="Survey demographics",
        tipo="municipio",
        municipio_id=owner.id,
        plan="full",
        is_active=True,
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
    )
    db.session.add(sender)
    db.session.commit()
    return tenant, sender


def _question(
    *,
    order: int,
    text: str,
    logical_ref: str | None,
    options: list[tuple[str, str | None]],
) -> EncPregunta:
    question = EncPregunta(
        orden=order,
        logical_ref=logical_ref,
        tipo="opcion_unica",
        texto=text,
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(orden=index, texto=label, valor=value)
        for index, (label, value) in enumerate(options, start=1)
    ]
    return question


def _published_survey(
    tenant: TenantProfile,
    *,
    questions: list[EncPregunta],
    source_anonymous: bool = False,
) -> EncEncuesta:
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=f"native-demographics-{uuid4().hex}",
        titulo="Consulta provincial",
        descripcion="Consulta institucional no vinculante.",
        estado="publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        politica_unicidad="libre" if source_anonymous else "por_cookie",
        anonimo_permitido=True,
        privacy_mode="source_anonymous" if source_anonymous else "legacy",
        privacy_policy_version="privacy-2026-08" if source_anonymous else None,
        privacy_policy_url=(
            "https://www.chatboc.ar/privacy/privacy-2026-08"
            if source_anonymous
            else None
        ),
        privacy_consent_required=source_anonymous,
        response_retention_days=365 if source_anonymous else None,
        puntos_recompensa=0,
    )
    survey.preguntas = questions
    db.session.add(survey)
    db.session.commit()
    return survey


def _interaction(
    tenant: TenantProfile,
    sender: ProviderSender,
    survey: EncEncuesta,
    selections: dict[int, int],
) -> WhatsAppFlowInteraction:
    suffix = uuid4().hex
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"survey-demographics-{suffix[:12]}",
        language="es",
        status="approved",
        content_sid=f"HX{suffix[:30]}",
        external_template_id=f"meta-{suffix[:20]}",
    )
    db.session.add(registry)
    db.session.flush()
    interaction = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=SURVEY_FLOW_ID,
        meta_flow_id=registry.external_template_id,
        content_sid=registry.content_sid,
        recipient_hash=uuid4().hex + uuid4().hex,
        recipient_hint="***1234",
        token_digest=uuid4().hex + uuid4().hex,
        idempotency_key=f"survey-demographics-{suffix}",
        status="sent",
        data_contract=["confirm_vote"],
        metadata_json={
            "survey_context": {
                "id": str(survey.id),
                "slug": survey.slug,
                "instrument_revision": int(survey.structure_revision or 1),
            },
            "survey_staged_answers": {
                str(question_id): option_id
                for question_id, option_id in selections.items()
            },
        },
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.session.add(interaction)
    db.session.commit()
    return interaction


def _submission(interaction: WhatsAppFlowInteraction) -> dict:
    return {
        "contract_version": "whatsapp.flow_submission.v1",
        "flow": {
            "id": interaction.flow_id,
            "meta_id": interaction.meta_flow_id,
        },
        "payload": {"answers": {"confirm_vote": True}},
        "correlation": {
            "interaction_id": interaction.id,
            "tenant_id": interaction.tenant_id,
            "provider_sender_id": interaction.provider_sender_id,
        },
    }


def test_whatsapp_flow_persists_explicit_city_and_province_and_replays_once(client):
    with client.application.app_context():
        tenant, sender = _tenant_scope()
        city = _question(
            order=1,
            text="Selecciona tu ciudad",
            logical_ref="demographic:city",
            options=[
                ("Capital provincial", "Ushuaia"),
                ("Zona norte", "Río Grande"),
            ],
        )
        province = _question(
            order=2,
            text="Selecciona tu provincia",
            logical_ref="demographic:province",
            options=[
                ("Provincia insular", "Tierra del Fuego"),
                ("Otra jurisdicción", "Otra provincia"),
            ],
        )
        vote = _question(
            order=3,
            text="¿Estás de acuerdo?",
            logical_ref="participation:vote",
            options=[("Sí", "yes"), ("No", "no")],
        )
        survey = _published_survey(
            tenant,
            questions=[city, province, vote],
        )
        interaction = _interaction(
            tenant,
            sender,
            survey,
            {
                city.id: city.opciones[1].id,
                province.id: province.opciones[0].id,
                vote.id: vote.opciones[0].id,
            },
        )
        submission = _submission(interaction)

        assert consume_whatsapp_flow_interaction(
            interaction_id=interaction.id,
            tenant_id=tenant.id,
            inbound_message_sid=f"SM-{uuid4().hex}",
            commit=False,
        ) is True
        first = apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=submission,
            anon_id="+5492901000000",
        )
        db.session.commit()
        replay = replay_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=submission,
        )

        saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
        assert saved.ciudad == "Río Grande"
        assert saved.provincia == "Tierra del Fuego"
        assert saved.lat is None
        assert saved.lng is None
        assert len(saved.detalles) == 3
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 1
        assert replay["entity"] == first["entity"]
        assert replay["message_body"] == first["message_body"]
        assert "realtime_event" not in replay


def test_location_words_without_semantic_ref_do_not_populate_demographics(client):
    with client.application.app_context():
        tenant, sender = _tenant_scope()
        city_like_text = _question(
            order=1,
            text="¿Vivís en Ushuaia o en Río Grande?",
            logical_ref="participation:district",
            options=[("Ushuaia", "Ushuaia"), ("Río Grande", "Río Grande")],
        )
        survey = _published_survey(tenant, questions=[city_like_text])
        interaction = _interaction(
            tenant,
            sender,
            survey,
            {city_like_text.id: city_like_text.opciones[0].id},
        )

        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction),
            anon_id="+5492901000001",
        )
        db.session.commit()

        saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
        assert saved.ciudad is None
        assert saved.provincia is None


@pytest.mark.parametrize("invalid_value", [None, "", "Ushuaia\nArgentina", "x" * 121])
def test_semantic_demographic_question_requires_safe_explicit_values(
    client,
    invalid_value,
):
    with client.application.app_context():
        tenant, _ = _tenant_scope()
        city = _question(
            order=1,
            text="Ciudad",
            logical_ref="demographic:city",
            options=[("Ushuaia", invalid_value), ("Río Grande", "Río Grande")],
        )
        survey = _published_survey(tenant, questions=[city])

        with pytest.raises(MetaFlowActionError) as exc_info:
            authorize_survey_context(
                tenant.id,
                {"id": str(survey.id), "slug": survey.slug},
            )

        assert exc_info.value.code == "survey_demographic_contract_invalid"


def test_source_anonymous_flow_does_not_infer_privacy_consent(client):
    with client.application.app_context():
        tenant, sender = _tenant_scope()
        city = _question(
            order=1,
            text="Ciudad",
            logical_ref="demographic:city",
            options=[("Ushuaia", "Ushuaia"), ("Río Grande", "Río Grande")],
        )
        survey = _published_survey(
            tenant,
            questions=[city],
            source_anonymous=True,
        )
        interaction = _interaction(
            tenant,
            sender,
            survey,
            {city.id: city.opciones[0].id},
        )

        with pytest.raises(MetaFlowActionError) as exc_info:
            apply_whatsapp_flow_completion(
                tenant_id=tenant.id,
                interaction_id=interaction.id,
                submission=_submission(interaction),
                anon_id="+5492901000002",
            )

        assert exc_info.value.code == "survey_privacy_consent_required"
        assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0
