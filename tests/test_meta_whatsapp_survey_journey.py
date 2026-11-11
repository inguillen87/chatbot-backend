"""Signed callback/worker survey journey with native admin reads and local ORM."""
from copy import deepcopy
import csv
from datetime import timezone
import io
import json
from typing import Any, NamedTuple

import pytest
from sqlalchemy.orm.attributes import flag_modified

from models import (
    ChatSessionContext, EncRespuesta, SurveyResponseEffect, SurveyResponseReceipt,
    WhatsAppOutboundAttempt, db,
)
from services import whatsapp_inbound_worker as worker
from services.constants import CONTEXTO_MUNICIPIO
from services.meta_whatsapp_survey import STATE_KEY
from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY
from tests.test_meta_whatsapp_crm_acceptance import crm_environment, _login
from tests.test_meta_whatsapp_inbound_worker import _post, _turn
from tests.test_meta_whatsapp_outbox import APP, CONTACT, PHONE, WABA
import tests.test_whatsapp_survey_conversation as survey_fixture


class Study(NamedTuple):
    environment: Any
    survey: Any
    client: Any
    headers: dict[str, str]

    def __repr__(self):
        return "<Local synthetic survey study>"


@pytest.fixture
def study(crm_environment):
    env = crm_environment
    env.tenant.slug = env.owner.tenant_slug = "junin"
    env.tenant.configuracion = {**(env.tenant.configuracion or {}), "encuestas_enabled": True}
    db.session.commit()
    client, headers = _login(env, env.owner)
    builder = survey_fixture.WhatsAppSurveyConversationTest(methodName="runTest")
    builder.app, builder.tenant, builder.owner, builder.client = env.app, env.tenant, env.owner, client
    builder._admin_headers = lambda: headers
    survey = builder._create_governed_survey(slug="native-meta-governed-vote")
    env.app.config.update(SURVEY_JURISDICTION_GATE_MODE="enforce_visibility",
                          SURVEY_JURISDICTION_GATE_TENANT_IDS=str(env.tenant.id))
    return Study(env, survey, client, headers)


def send(env, body, suffix, *, contact=CONTACT, completed=True):
    turn_id = _post(env, body=body, message_id=f"wamid.survey/{suffix}+=", contact=contact)
    result = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    row = _turn(turn_id)
    if completed:
        assert result["completed"] == 1, result
    attempts = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).order_by(WhatsAppOutboundAttempt.id).all()
    bodies = [attempt.payload_json["message"]["text"]["body"] for attempt in attempts]
    assert all(attempt.provider == "meta" and attempt.status == "pending" for attempt in attempts)
    return row, bodies


def context(row):
    return db.session.get(ChatSessionContext, row.chat_session_id)


def flow(row):
    return deepcopy(context(row).context_data[CONTEXTO_MUNICIPIO][WHATSAPP_SURVEY_FLOW_STATE_KEY])


def begin(env, prefix):
    row, listing = send(env, "Encuestas", f"{prefix}-list")
    assert "1." in listing[0] and "encuesta" in listing[0].casefold()
    row, consent = send(env, "1", f"{prefix}-select")
    pinned = flow(row)
    assert pinned["step"] == "consent" and pinned["consent_accepted"] is False
    assert "Declaro que participo" in "".join(consent[:-1])
    assert "Respondé 1 para aceptar" in consent[-1]
    assert "audio adjunto" not in "".join(consent)
    row, question = send(env, "1", f"{prefix}-consent")
    assert flow(row)["step"] == "question" and "Pregunta 1" in question[0]
    return row, pinned["submission_id"]


def counts(survey):
    return (EncRespuesta.query.filter_by(encuesta_id=survey.id).count(),
            SurveyResponseReceipt.query.filter_by(survey_id=survey.id).count(),
            SurveyResponseEffect.query.filter_by(survey_id=survey.id).count())


@pytest.mark.parametrize("phase", ("before_consent", "after_last_question"))
@pytest.mark.parametrize("gate", ("strict_tenant", "malformed_allowlist"))
def test_assurance_blocks_native_survey_at_the_next_turn(study, phase, gate):
    env, survey, _, _ = study
    if phase == "before_consent":
        send(env, "Encuestas", "assurance-list")
    else:
        begin(env, "assurance-active")
        row, _ = send(env, "1", "assurance-first-answer")
        assert flow(row)["question_index"] == 1

    scoped = gate == "strict_tenant"
    env.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=True,
        SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS=str(env.tenant.id) if scoped else "",
    )
    public = env.app.test_client().get(
        f"/api/public/encuestas/v1/{survey.slug}", query_string={"tenant_slug": env.tenant.slug},
    )
    assert public.status_code == 200, public.get_json()
    assurance = public.get_json()["participation_assurance"]
    assert assurance["configuration_ready"] is False
    assert assurance["blocked_reason_code"] == (
        "survey_authoritative_participation_required" if scoped
        else "survey_participation_assurance_misconfigured"
    )

    choice = "1" if phase == "before_consent" else "2"
    row, output = send(env, choice, "assurance-blocked-turn", completed=False)
    assert (row.status, row.last_error_code) == ("completed", None)
    body = "\n".join(output)
    assert "revisión institucional" in body and "No se registró ninguna respuesta" in body
    assert "Respondé 1 para aceptar" not in body and "Pregunta " not in body
    assert "/e/" not in body
    assert counts(survey) == (0, 0, 0)
    assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context(row).context_data[CONTEXTO_MUNICIPIO]


def test_assurance_allowlist_does_not_interrupt_other_tenants(study):
    env, survey, _, _ = study
    env.app.config.update(
        ENABLE_SURVEY_PARTICIPATION_ASSURANCE_V1=True,
        SURVEY_PARTICIPATION_ASSURANCE_TENANT_IDS=str(env.other.id),
    )
    begin(env, "assurance-other-tenant")
    send(env, "1", "assurance-other-first")
    _, reply = send(env, "2", "assurance-other-last")
    assert "Participación registrada" in reply[0]
    assert counts(survey) == (1, 1, 2)


def test_signed_survey_roundtrip_receipt_effects_admin_counts_and_duplicate_participation(study):
    env, survey, client, headers = study
    row, submission = begin(env, "first")
    session = context(row)
    session.context_data[CONTEXTO_MUNICIPIO]["contacto_usuario"] = {"telefono": "+5491198765432"}
    flag_modified(session, "context_data")
    db.session.commit()
    row, _ = send(env, "1", "first-answer")
    assert flow(row)["question_index"] == 1 and counts(survey) == (0, 0, 0)
    row, receipt = send(env, "2", "last-answer")
    assert "Participación registrada" in receipt[0]
    assert counts(survey) == (1, 1, 2)
    assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context(row).context_data[CONTEXTO_MUNICIPIO]
    response = EncRespuesta.query.filter_by(encuesta_id=survey.id).one()
    assert response.phone == "+" + CONTACT and response.user_id is None
    assert response.canal == "whatsapp_chat" and len(response.detalles) == 2

    before_attempts = WhatsAppOutboundAttempt.query.count()
    duplicate = _post(env, body="2", message_id="wamid.survey/last-answer+=")
    assert duplicate == row.turn_id
    repeated = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert repeated["completed"] == 0 and counts(survey) == (1, 1, 2)
    assert WhatsAppOutboundAttempt.query.count() == before_attempts

    row, next_submission = begin(env, "second")
    assert next_submission != submission
    send(env, "1", "second-answer")
    row, duplicate_reply = send(env, "2", "second-last")
    assert "Ya existe una participación" in duplicate_reply[0]
    assert counts(survey) == (1, 1, 2)
    assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context(row).context_data[CONTEXTO_MUNICIPIO]

    listing = client.get(f"/api/admin/encuestas/{survey.id}/respuestas", headers=headers)
    assert listing.status_code == 200, listing.get_json()
    assert listing.get_json()["total"] == 1 and len(listing.get_json()["respuestas"]) == 1
    summary = client.get(f"/api/encuestas/{survey.id}/analytics/summary", headers=headers)
    assert summary.status_code == 200, summary.get_json()
    assert summary.get_json()["total_respuestas"] == 1
    exported = client.get(f"/api/encuestas/{survey.id}/analytics/export.csv", headers=headers)
    assert exported.status_code == 200 and exported.mimetype == "text/csv"
    exported_rows = list(csv.DictReader(io.StringIO(exported.get_data(as_text=True))))
    assert len(exported_rows) == 1


@pytest.mark.parametrize("failure", ("reply", "postwrite_duplicate"))
def test_final_write_and_state_roll_back_with_all_effects_when_reply_or_postwrite_fails(study, monkeypatch, failure):
    env, survey, _, _ = study
    begin(env, "rollback")
    row, _ = send(env, "1", "rollback-first")
    previous = flow(row)
    if failure == "reply":
        from services.meta_whatsapp_conversation_sink import MetaConversationSink
        original = MetaConversationSink.emit_text

        def reject_receipt(self, body):
            if "Participación registrada" in body:
                self.unit.fail("synthetic_survey_reply_rejected")
            return original(self, body)

        monkeypatch.setattr(MetaConversationSink, "emit_text", reject_receipt)
    else:
        from services import whatsapp_survey_conversation as conversation
        from services.encuestas_service import EncuestaError
        save = conversation.save_respuesta
        staged = []

        def reject_after_write(*args, **kwargs):
            save(*args, **kwargs)
            staged.append(counts(survey))
            raise EncuestaError("Synthetic late rejection", 409, {"reason_code": "survey_response_duplicate"})

        monkeypatch.setattr(conversation, "save_respuesta", reject_after_write)
    row, bodies = send(env, "2", "rollback-last", completed=False)
    assert row.status == ("dead" if failure == "reply" else "retry_wait") and bodies == []
    assert row.last_error_code == ("synthetic_survey_reply_rejected" if failure == "reply" else "EncuestaError")
    assert counts(survey) == (0, 0, 0)
    assert flow(row) == previous
    if failure == "postwrite_duplicate":
        from services import whatsapp_inbound_turns as queue
        assert staged == [(1, 1, 2)]
        retry_at = row.available_at.replace(tzinfo=row.available_at.tzinfo or timezone.utc)
        env.clock[0] = int(retry_at.timestamp())
        monkeypatch.setattr(queue, "_utc_now", lambda: retry_at)
        monkeypatch.setattr(conversation, "save_respuesta", save)
        retry = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
        assert retry["completed"] == 1 and counts(survey) == (1, 1, 2)
        assert _turn(row.turn_id).status == "completed"


def test_uncertain_acknowledgement_after_real_commit_never_replays_the_vote(study, monkeypatch):
    from services.whatsapp_conversation_uow import ConversationCommitUncertain, ConversationUnitOfWork

    env, survey, _, _ = study
    begin(env, "uncertain")
    send(env, "1", "uncertain-first")
    commit = ConversationUnitOfWork.commit_reply

    def lose_acknowledgement(unit, *args, **kwargs):
        commit(unit, *args, **kwargs)
        raise ConversationCommitUncertain("synthetic_survey_commit_acknowledgement_lost")

    monkeypatch.setattr(ConversationUnitOfWork, "commit_reply", lose_acknowledgement)
    turn_id = _post(env, body="2", message_id="wamid.survey/uncertain-last+=")
    reported = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)["results"][0]
    assert reported["status"] == "commit_uncertain"
    row = _turn(turn_id)
    assert row.status == "completed" and counts(survey) == (1, 1, 2)
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).count() == 1
    assert worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)["processed"] == 0


@pytest.mark.parametrize("change", ("disabled", "closed", "withdrawn_boundary", "foreign_navigation"))
def test_dependent_turn_rechecks_current_tenant_publication_and_navigation_scope(study, change):
    env, survey, _, _ = study
    row, _ = begin(env, "retired")
    if change == "disabled":
        env.tenant.configuracion = {**env.tenant.configuracion, "encuestas_enabled": False}
    elif change == "closed":
        survey.estado = "cerrada"
    elif change == "withdrawn_boundary":
        env.tenant.jurisdiction_status = "unverified"
        env.tenant.jurisdiction_evidence_ref = None
    else:
        item = context(row)
        item.context_data[CONTEXTO_MUNICIPIO][STATE_KEY]["scope"]["tenant_id"] = env.other.id
        flag_modified(item, "context_data")
    db.session.commit()
    row, output = send(env, "1", "retired-answer")
    assert counts(survey) == (0, 0, 0)
    assert "Participación registrada" not in "".join(output)
    assert WHATSAPP_SURVEY_FLOW_STATE_KEY not in context(row).context_data[CONTEXTO_MUNICIPIO]


def test_other_contact_has_no_access_to_active_survey_answers(study):
    env, survey, _, _ = study
    row, _ = begin(env, "participant")
    previous = flow(row)
    second = "5491198765432"
    env.app.config["META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON"] = json.dumps([
        {"app_id": APP, "waba_id": WABA, "phone_number_id": PHONE,
         "tenant_id": env.tenant.id, "contact": contact}
        for contact in (CONTACT, second)])
    other, output = send(env, "1", "other-answer", contact=second, completed=False)
    assert other.chat_session_id != row.chat_session_id and output == []
    assert flow(row) == previous and counts(survey) == (0, 0, 0)


def test_false_writer_capability_cannot_enable_external_submission(study):
    from services.encuestas_service import save_respuesta
    from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
    env, survey, _, _ = study
    with pytest.raises(ConversationUnitOfWorkError, match="survey_conversation_writer_scope_invalid"):
        save_respuesta(survey.slug, {}, {}, commit=False, require_tenant_match=True,
                       preferred_tenant_id=env.tenant.id, unit_of_work=True,
                       conversation_turn_id="synthetic-turn")
    assert counts(survey) == (0, 0, 0)


def test_live_owner_cannot_write_a_vote_without_its_receipt_key(study, monkeypatch):
    from services import whatsapp_survey_conversation as conversation
    from services.encuestas_service import EncuestaError

    env, survey, _, _ = study
    begin(env, "missing-key")
    row, _ = send(env, "1", "missing-key-first")
    previous = flow(row)
    save = conversation.save_respuesta
    rejections = []

    def omit_key(slug, payload, request_context, **options):
        payload = {name: value for name, value in payload.items() if name != "submission_id"}
        options.pop("submission_id")
        try:
            return save(slug, payload, request_context, **options)
        except EncuestaError as exc:
            rejections.append(exc.payload["reason_code"])
            raise

    monkeypatch.setattr(conversation, "save_respuesta", omit_key)
    row, bodies = send(env, "2", "missing-key-last", completed=False)
    assert rejections == ["survey_submission_id_required"]
    assert row.status == "retry_wait" and bodies == []
    assert counts(survey) == (0, 0, 0) and flow(row) == previous
