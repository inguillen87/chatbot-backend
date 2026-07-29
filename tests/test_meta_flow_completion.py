from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app import db
from models import (
    AnalyticsEventV2,
    AuditEvent,
    EncEncuesta,
    EncOpcion,
    EncPregunta,
    EncRespuesta,
    MessageTemplateRegistry,
    MunicipioTicket,
    Order,
    ProviderSender,
    SurveyResponseEffect,
    TenantProfile,
    TicketComentario,
    User,
    WhatsAppFlowInteraction,
)
from services.meta_flow_data_exchange import MetaFlowActionError
import services.encuestas_service as encuesta_service
from services.meta_flow_runtime import (
    CLAIM_FLOW_ID,
    ORDER_FLOW_ID,
    SURVEY_FLOW_ID,
    apply_whatsapp_flow_completion,
)
from services.survey_response_effects import dispatch_survey_response_effects
from services.whatsapp_flow_security import consume_whatsapp_flow_interaction


def _tenant_scope(slug: str) -> tuple[TenantProfile, ProviderSender]:
    owner = User(
        email=f"{slug}@example.test",
        name=slug,
        rol="tenant_admin",
        tipo_chat="municipio",
    )
    owner.set_password("test-pass")
    db.session.add(owner)
    db.session.flush()
    tenant = TenantProfile(
        slug=slug,
        nombre=slug.title(),
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


def _interaction(
    *,
    tenant: TenantProfile,
    sender: ProviderSender,
    flow_id: str,
    metadata: dict,
    data_contract: list[str],
) -> WhatsAppFlowInteraction:
    registry = MessageTemplateRegistry(
        tenant_id=tenant.id,
        provider="twilio",
        channel="whatsapp",
        name=f"completion-{flow_id}-{tenant.id}",
        language="es",
        status="approved",
        content_sid=f"HXCOMPLETION{tenant.id}",
        external_template_id=f"meta-{tenant.id}",
    )
    db.session.add(registry)
    db.session.flush()
    row = WhatsAppFlowInteraction(
        tenant_id=tenant.id,
        template_registry_id=registry.id,
        provider_sender_id=sender.id,
        flow_id=flow_id,
        meta_flow_id=registry.external_template_id,
        content_sid=registry.content_sid,
        recipient_hash=f"{tenant.id:064x}"[-64:],
        recipient_hint="***1234",
        token_digest=f"{tenant.id + 500:064x}"[-64:],
        idempotency_key=f"completion-{flow_id}-{tenant.id}",
        status="sent",
        data_contract=data_contract,
        metadata_json=metadata,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _submission(interaction: WhatsAppFlowInteraction, answers: dict) -> dict:
    return {
        "contract_version": "whatsapp.flow_submission.v1",
        "flow": {"id": interaction.flow_id, "meta_id": interaction.meta_flow_id},
        "payload": {"answers": answers},
        "correlation": {
            "interaction_id": interaction.id,
            "tenant_id": interaction.tenant_id,
            "provider_sender_id": interaction.provider_sender_id,
        },
    }


def _published_quick_vote(tenant: TenantProfile, *, slug: str) -> EncEncuesta:
    survey = EncEncuesta(
        tenant_id=tenant.id,
        slug=slug,
        titulo="Prioridades del barrio",
        estado="publicada",
        tipo="votacion",
        es_votacion_envivo=True,
        mostrar_resultados_envivo=True,
        politica_unicidad="por_cookie",
    )
    question = EncPregunta(
        orden=1,
        tipo="opcion_unica",
        texto="Que mejora deberia priorizarse?",
        obligatoria=True,
    )
    question.opciones = [
        EncOpcion(orden=1, texto="Iluminacion"),
        EncOpcion(orden=2, texto="Arreglo de calles"),
    ]
    survey.preguntas = [question]
    db.session.add(survey)
    db.session.commit()
    return survey


def test_claim_completion_consumes_once_and_writes_crm_comment(client):
    tenant, sender = _tenant_scope("completion-claim")
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket="378430",
        consulta_pin="900144",
        pregunta="Arreglo de calle",
        categoria="Arreglo de calle",
        estado="en_proceso",
    )
    db.session.add(ticket)
    db.session.commit()
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=CLAIM_FLOW_ID,
        metadata={
            "claim_context": {
                "kind": "municipio",
                "id": str(ticket.id),
                "ticket_number": "M-378430",
            }
        },
        data_contract=["ticket_number", "follow_up_note"],
    )
    submission = _submission(
        interaction,
        {
            "ticket_number": "M-378430",
            "follow_up_note": "La calle sigue cortada; por favor revisar.",
        },
    )

    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-FLOW-CLAIM-1",
        commit=False,
    ) is True
    response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=submission,
        anon_id="+5491112345678",
    )
    db.session.commit()

    comment = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).one()
    db.session.refresh(interaction)
    assert comment.comentario == "La calle sigue cortada; por favor revisar."
    assert comment.origen == "whatsapp_flow"
    assert response["fuente"] == "whatsapp_flow_claim_completed"
    assert response["realtime_event"]["comment_id"] == comment.id
    assert interaction.status == "consumed"
    assert interaction.metadata_json["completion"]["status"] == "applied"
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{CLAIM_FLOW_ID}.completed",
    ).count() == 1
    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-FLOW-CLAIM-2",
    ) is False


def test_claim_completion_rejects_ticket_not_authorized_by_data_exchange(client):
    tenant, sender = _tenant_scope("completion-claim-scope")
    ticket = MunicipioTicket(
        tenant_id=tenant.id,
        municipio_id=tenant.municipio_id,
        nro_ticket="100001",
        consulta_pin="445566",
        pregunta="Luminaria",
        estado="nuevo",
    )
    db.session.add(ticket)
    db.session.commit()
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=CLAIM_FLOW_ID,
        metadata={
            "claim_context": {
                "kind": "municipio",
                "id": str(ticket.id),
                "ticket_number": "M-100001",
            }
        },
        data_contract=["ticket_number", "follow_up_note"],
    )

    with pytest.raises(MetaFlowActionError) as error:
        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(
                interaction,
                {
                    "ticket_number": "M-999999",
                    "follow_up_note": "Intento fuera de alcance",
                },
            ),
        )

    assert error.value.code == "claim_context_scope_mismatch"
    assert TicketComentario.query.count() == 0


def test_order_completion_uses_server_order_and_preserves_financial_authority(client):
    tenant, sender = _tenant_scope("completion-order")
    order = Order(
        id="order-flow-001",
        tenant_id=tenant.id,
        buyer_name="Nombre anterior",
        status="created",
        channel="web_widget",
        currency="ARS",
        subtotal=25000,
        total=25000,
    )
    db.session.add(order)
    db.session.commit()
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=ORDER_FLOW_ID,
        metadata={"order_context": {"kind": "order", "id": order.id}},
        data_contract=[
            "full_name",
            "phone",
            "delivery_address",
            "delivery_notes",
            "confirm_order",
        ],
    )
    submission = _submission(
        interaction,
        {
            "full_name": "Cliente Final",
            "phone": "+5491112345678",
            "delivery_address": "Calle 123, Junin",
            "delivery_notes": "Entregar de 9 a 13",
            "confirm_order": True,
            "order_id": "attacker-order",
            "total": 1,
            "payment_state": "paid",
        },
    )

    response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=submission,
    )
    db.session.commit()

    db.session.refresh(order)
    assert response["fuente"] == "whatsapp_flow_order_completed"
    assert order.id == "order-flow-001"
    assert order.buyer_name == "Cliente Final"
    assert order.buyer_phone == "+5491112345678"
    assert order.delivery_address == {
        "address": "Calle 123, Junin",
        "source": "whatsapp_flow",
    }
    assert order.total == 25000
    assert order.status == "confirmed"
    assert order.channel == "whatsapp"


def test_order_completion_is_idempotent_and_cannot_rewrite_applied_data(client):
    tenant, sender = _tenant_scope("completion-order-replay")
    order = Order(
        id="order-flow-replay",
        tenant_id=tenant.id,
        status="pending_payment",
        total=9000,
    )
    db.session.add(order)
    db.session.commit()
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=ORDER_FLOW_ID,
        metadata={"order_context": {"kind": "order", "id": order.id}},
        data_contract=[
            "full_name",
            "phone",
            "delivery_address",
            "delivery_notes",
            "confirm_order",
        ],
    )
    first = _submission(
        interaction,
        {
            "full_name": "Primer Cliente",
            "phone": "+5491112345678",
            "delivery_address": "Direccion valida 10",
            "delivery_notes": "Sin timbre",
            "confirm_order": True,
        },
    )
    apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=first,
    )
    db.session.commit()

    replay = _submission(
        interaction,
        {
            "full_name": "Nombre alterado",
            "phone": "+5491199999999",
            "delivery_address": "Otra direccion",
            "confirm_order": True,
        },
    )
    replay_response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=replay,
    )
    db.session.commit()

    db.session.refresh(order)
    assert replay_response["fuente"] == "whatsapp_flow_order_completed"
    assert order.buyer_name == "Primer Cliente"
    assert order.buyer_phone == "+5491112345678"
    assert order.delivery_address["address"] == "Direccion valida 10"
    assert order.status == "pending_payment"
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{ORDER_FLOW_ID}.completed",
    ).count() == 1


def test_survey_completion_persists_one_canonical_vote_and_is_idempotent(client):
    tenant, sender = _tenant_scope("completion-survey")
    survey = _published_quick_vote(tenant, slug="completion-survey-vote")
    question = survey.preguntas[0]
    selected = question.opciones[1]
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {"id": str(survey.id), "slug": survey.slug},
            "survey_staged_answers": {str(question.id): selected.id},
        },
        data_contract=["confirm_vote"],
    )
    submission = _submission(interaction, {"confirm_vote": True})

    assert consume_whatsapp_flow_interaction(
        interaction_id=interaction.id,
        tenant_id=tenant.id,
        inbound_message_sid="SM-FLOW-SURVEY-1",
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
    db.session.refresh(interaction)
    assert response["fuente"] == "whatsapp_flow_survey_completed"
    assert response["entity"] == {"kind": "survey_response", "id": saved.id}
    assert response["realtime_event"] == {
        "kind": "survey_vote",
        "survey_id": survey.id,
        "survey_slug": survey.slug,
    }
    assert response["options_list"][0] == {
        "texto": "Ver resultados",
        "type": "url",
        "url": f"https://www.chatboc.ar/e/{survey.slug}?resultados=1",
        "action_id": "open_survey_results",
    }
    assert saved.canal == "whatsapp_flow"
    assert saved.phone == "+5491112345678"
    assert saved.metadata_payload["interaction_id"] == interaction.id
    assert len(saved.detalles) == 1
    assert saved.detalles[0].pregunta_id == question.id
    assert saved.detalles[0].opcion_id == selected.id
    assert interaction.status == "consumed"
    assert interaction.metadata_json["completion"]["status"] == "applied"
    staged_effects = SurveyResponseEffect.query.filter_by(
        tenant_id=tenant.id,
        response_id=saved.id,
    ).all()
    assert len(staged_effects) == 2
    assert {effect.status for effect in staged_effects} == {"pending"}
    with patch(
        "services.encuestas_service.emit_survey_response_update",
        return_value=True,
    ):
        dispatch_result = dispatch_survey_response_effects(
            tenant_id=tenant.id,
            response_id=saved.id,
            limit=3,
        )
    assert dispatch_result["succeeded"] == 2
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="vote_submitted",
        entity_ref=f"survey:{survey.id}:response:{saved.id}",
    ).count() == 1
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{SURVEY_FLOW_ID}.completed",
        resource_id=str(saved.id),
    ).count() == 1

    replay = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=submission,
        anon_id="+5491112345678",
    )
    db.session.commit()

    assert replay["entity"] == {"kind": "survey_response", "id": str(saved.id)}
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 1
    assert SurveyResponseEffect.query.filter_by(
        tenant_id=tenant.id,
        survey_id=survey.id,
    ).count() == 2
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="vote_submitted",
    ).count() == 1
    assert AuditEvent.query.filter_by(
        tenant_id=tenant.id,
        event_type=f"whatsapp_flow.{SURVEY_FLOW_ID}.completed",
    ).count() == 1


def test_survey_completion_persists_only_current_visible_branch(client):
    tenant, sender = _tenant_scope("completion-survey-adaptive")
    survey = _published_quick_vote(
        tenant,
        slug="completion-survey-adaptive-vote",
    )
    gate = survey.preguntas[0]
    gate.logical_ref = "priority-gate"
    gate.opciones[0].logical_ref = "priority-yes"
    gate.opciones[1].logical_ref = "priority-no"
    follow_up = EncPregunta(
        orden=2,
        tipo="opcion_unica",
        texto="Detalle de la prioridad",
        obligatoria=True,
        logical_ref="priority-detail",
        logica_condicional={
            "version": 2,
            "show_if": {
                "kind": "group",
                "operator": "and",
                "children": [
                    {
                        "kind": "option_selected",
                        "question_ref": "priority-gate",
                        "option_ref": "priority-yes",
                    }
                ],
            },
        },
    )
    follow_up.opciones = [
        EncOpcion(orden=1, texto="Centro", logical_ref="detail-center"),
        EncOpcion(orden=2, texto="Barrios", logical_ref="detail-neighborhoods"),
    ]
    outcome = EncPregunta(
        orden=3,
        tipo="opcion_unica",
        texto="Como queres continuar?",
        obligatoria=True,
        logical_ref="priority-outcome",
        logica_condicional={
            "version": 2,
            "show_if": {
                "kind": "group",
                "operator": "or",
                "children": [
                    {
                        "kind": "group",
                        "operator": "and",
                        "children": [
                            {
                                "kind": "option_selected",
                                "question_ref": "priority-gate",
                                "option_ref": "priority-yes",
                            },
                            {
                                "kind": "option_selected",
                                "question_ref": "priority-detail",
                                "option_ref": "detail-center",
                            },
                        ],
                    },
                    {
                        "kind": "option_selected",
                        "question_ref": "priority-gate",
                        "option_ref": "priority-no",
                    },
                ],
            },
        },
    )
    outcome.opciones = [
        EncOpcion(orden=1, texto="Enviar", logical_ref="outcome-send"),
        EncOpcion(orden=2, texto="Revisar", logical_ref="outcome-review"),
    ]
    survey.preguntas.extend([follow_up, outcome])
    db.session.commit()
    skip_branch = gate.opciones[1]
    stale_hidden_answer = follow_up.opciones[0]
    visible_outcome_answer = outcome.opciones[0]
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {
                "id": str(survey.id),
                "slug": survey.slug,
                "instrument_revision": 1,
            },
            "survey_staged_answers": {
                str(gate.id): skip_branch.id,
                str(follow_up.id): stale_hidden_answer.id,
                str(outcome.id): visible_outcome_answer.id,
            },
        },
        data_contract=["confirm_vote"],
    )

    response = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction.id,
        submission=_submission(interaction, {"confirm_vote": True}),
        anon_id="+5491112345678",
    )
    db.session.commit()

    saved = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
    db.session.refresh(interaction)
    assert response["fuente"] == "whatsapp_flow_survey_completed"
    assert {
        (detail.pregunta_id, detail.opcion_id) for detail in saved.detalles
    } == {
        (gate.id, skip_branch.id),
        (outcome.id, visible_outcome_answer.id),
    }
    assert saved.metadata_payload["instrument_revision"] == 1
    assert saved.metadata_payload["adaptive_navigation"] is True
    assert saved.metadata_payload["visible_question_ids"] == [gate.id, outcome.id]
    assert interaction.metadata_json["survey_staged_answers"] == {
        str(gate.id): skip_branch.id,
        str(outcome.id): visible_outcome_answer.id,
    }
    assert interaction.metadata_json["survey_navigation"]["visible_question_ids"] == [
        gate.id,
        outcome.id,
    ]


def test_survey_completion_nested_savepoint_is_rolled_back_with_outer_transaction_on_sqlite(client):
    tenant, sender = _tenant_scope("completion-survey-outer-rollback")
    survey = _published_quick_vote(
        tenant,
        slug="completion-survey-outer-rollback-vote",
    )
    question = survey.preguntas[0]
    selected = question.opciones[0]
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {"id": str(survey.id), "slug": survey.slug},
            "survey_staged_answers": {str(question.id): selected.id},
        },
        data_contract=["confirm_vote"],
    )
    survey_id = survey.id
    tenant_id = tenant.id
    interaction_id = interaction.id

    result = apply_whatsapp_flow_completion(
        tenant_id=tenant.id,
        interaction_id=interaction_id,
        submission=_submission(interaction, {"confirm_vote": True}),
        anon_id="+5491112345678",
    )

    # The nested write is visible inside the caller-owned transaction.
    assert result["entity"]["kind"] == "survey_response"
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 1
    assert SurveyResponseEffect.query.filter_by(
        tenant_id=tenant_id,
        survey_id=survey_id,
    ).count() == 2
    assert db.session.get(EncEncuesta, survey_id).structure_locked_at is not None

    # A later failure in invocation consumption/orchestration must undo the
    # response and its durable structure marker together.
    db.session.rollback()
    db.session.remove()
    assert EncRespuesta.query.filter_by(encuesta_id=survey_id).count() == 0
    assert SurveyResponseEffect.query.filter_by(
        tenant_id=tenant_id,
        survey_id=survey_id,
    ).count() == 0
    assert db.session.get(EncEncuesta, survey_id).structure_locked_at is None
    reloaded_interaction = db.session.get(WhatsAppFlowInteraction, interaction_id)
    assert (reloaded_interaction.metadata_json or {}).get("completion") is None
    assert AuditEvent.query.filter_by(
        tenant_id=tenant_id,
        event_type=f"whatsapp_flow.{SURVEY_FLOW_ID}.completed",
    ).count() == 0


def test_survey_completion_preserves_concurrency_reason_instead_of_claiming_duplicate(
    client,
    monkeypatch,
):
    tenant, sender = _tenant_scope("completion-survey-concurrent")
    survey = _published_quick_vote(tenant, slug="completion-survey-concurrent-vote")
    question = survey.preguntas[0]
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {"id": str(survey.id), "slug": survey.slug},
            "survey_staged_answers": {
                str(question.id): question.opciones[0].id
            },
        },
        data_contract=["confirm_vote"],
    )

    def concurrent_update(*_args, **_kwargs):
        raise encuesta_service.EncuestaError(
            "La encuesta esta siendo actualizada",
            status_code=409,
            payload={
                "reason_code": "survey_concurrent_update",
                "retryable": True,
                "action_hint": "reload_survey",
            },
        )

    monkeypatch.setattr(encuesta_service, "save_respuesta", concurrent_update)
    with pytest.raises(MetaFlowActionError) as exc_info:
        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction, {"confirm_vote": True}),
            anon_id="+5491112345678",
        )
    db.session.rollback()

    assert exc_info.value.status_code == 409
    assert exc_info.value.code == "survey_concurrent_update"
    assert exc_info.value.code != "survey_already_answered"
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0
    assert SurveyResponseEffect.query.filter_by(
        tenant_id=tenant.id,
        survey_id=survey.id,
    ).count() == 0


def test_survey_completion_rejects_missing_staged_answers(client):
    tenant, sender = _tenant_scope("completion-survey-incomplete")
    survey = _published_quick_vote(tenant, slug="completion-survey-incomplete-vote")
    interaction = _interaction(
        tenant=tenant,
        sender=sender,
        flow_id=SURVEY_FLOW_ID,
        metadata={
            "survey_context": {"id": str(survey.id), "slug": survey.slug},
            "survey_staged_answers": {},
        },
        data_contract=["confirm_vote"],
    )

    with pytest.raises(MetaFlowActionError) as error:
        apply_whatsapp_flow_completion(
            tenant_id=tenant.id,
            interaction_id=interaction.id,
            submission=_submission(interaction, {"confirm_vote": True}),
            anon_id="+5491112345678",
        )

    assert error.value.code == "survey_answers_incomplete"
    assert EncRespuesta.query.filter_by(encuesta_id=survey.id).count() == 0
    assert SurveyResponseEffect.query.filter_by(
        tenant_id=tenant.id,
        survey_id=survey.id,
    ).count() == 0
    assert AnalyticsEventV2.query.filter_by(
        tenant_id=tenant.id,
        event_name="vote_submitted",
    ).count() == 0
