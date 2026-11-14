"""Prepared signed Meta/PyME text journeys; all identities/configuration are synthetic.

These use the repository's real Flask/SQLite callback and conversation UOW fixture.
They do not send messages to Meta, provision accounts or establish a production CRM bridge.
"""
from copy import deepcopy

import pytest

from models import ChatSessionContext, MunicipioTicket, TenantConfig, WhatsAppOutboundAttempt, db
from models_memory import Contact, ContactSnapshot, InteractionEvent
from services import crm_intelligence as crm
from services import whatsapp_inbound_worker as worker
from services.whatsapp_conversation_uow import ConversationUnitOfWork
from services.constants import CONTEXTO_MUNICIPIO
from services.meta_whatsapp_survey import STATE_KEY as SURVEY_NAVIGATION_KEY
from services.whatsapp_survey_conversation import WHATSAPP_SURVEY_FLOW_STATE_KEY
from tests.test_meta_whatsapp_crm_acceptance import crm_environment, _login, _native_turn, _ok
from tests.test_meta_whatsapp_inbound_worker import _post
from tests.test_meta_whatsapp_outbox import CONTACT


STATE_KEY = "meta_native_text_v1"
CONTEXT_KEY = "contexto_pyme_v2"
ACK = "Recibimos tu consulta."
EVIDENCE_CONTRACT = "crm.meta_native_business_evidence.v1"


@pytest.fixture
def business_environment(crm_environment):
    env = crm_environment
    env.tenant.tipo = "pyme"
    env.tenant.pyme_id = env.owner.id
    env.tenant.municipio_id = None
    env.tenant.vertical = "empresa"
    env.tenant.configuracion = {}
    env.owner.tipo_chat = "pyme"
    env.owner.pyme_id = env.owner.id
    env.owner.municipio_id = None
    db.session.commit()
    return env


def _menu(prefix="Specific"):
    return {
        "enabled": True,
        "meta_native_text": {
            "contract_version": "tenant.meta_native_text.v1",
            "status": "configured",
            "messages": {
                "welcome": prefix + " synthetic welcome",
                "inquiry_received": ACK,
                "inquiry_continued": ACK,
                "service_unavailable": prefix + " synthetic service unavailable",
            },
            "services": [{"id": "service-a", "title": prefix + " service A",
                          "description": prefix + " synthetic service description"}],
        },
    }


def _install_menu(env, value, *, channel="whatsapp", tenant=None):
    target = tenant or env.tenant
    row = TenantConfig(tenant_id=target.id, key="menu", channel=channel, json_value=deepcopy(value))
    db.session.add(row)
    db.session.commit()
    return row


def _text_outbox(turn):
    rows = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=turn.id).order_by(WhatsAppOutboundAttempt.id).all()
    assert all(row.provider == "meta" and row.status == "pending" for row in rows)
    return [row.payload_json["message"]["text"]["body"] for row in rows]


def _send(env, body, suffix):
    turn_id = _post(env, body=body, message_id="wamid.pyme/" + suffix + "+=")
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    turn = _native_turn(turn_id)
    assert report["completed"] == 1 and turn.status == "completed", report
    assert env.app.config["META_WHATSAPP_OUTBOUND_ENABLED"] is False
    assert MunicipioTicket.query.count() == 0
    return turn, _text_outbox(turn), report


def _state(turn):
    db.session.expire_all()
    data = db.session.get(ChatSessionContext, turn.chat_session_id).context_data
    return deepcopy(data[CONTEXT_KEY][STATE_KEY])


def _own_events(env):
    return InteractionEvent.query.filter_by(tenant_id=env.tenant.id).order_by(InteractionEvent.id).all()


def _assert_raw_event(env, turn, body):
    events = _own_events(env)
    event = next(row for row in events if row.metadata_payload["durable_turn_id"] == turn.turn_id)
    contact = db.session.get(Contact, event.contact_id)
    assert event.content == body and event.content_type == "text"
    assert (event.channel, event.direction, event.tenant_id) == ("whatsapp", "inbound", env.tenant.id)
    assert contact.tenant_id == env.tenant.id and contact.phone == "+" + CONTACT
    assert event.metadata_payload["provider"] == "meta"
    assert event.metadata_payload["source"] == "whatsapp_webhook"
    assert event.metadata_payload["event_type"] == "whatsapp_inbound"
    assert event.metadata_payload["message_sid"] == turn.provider_message_sid
    assert event.metadata_payload["source_event_id"] == turn.provider_message_sid
    assert event.metadata_payload["idempotency_key"]
    assert ContactSnapshot.query.count() == 0
    assert "marketing_opt_in" not in (contact.preferences or {})
    assert contact.preferences["native_business_evidence_contract"] == EVIDENCE_CONTRACT
    assert "lead_score" not in contact.preferences and "lead_temperature" not in contact.preferences
    for forbidden in ("raw_payload", "access_token", "media_url", "intent", "lead_score"):
        assert forbidden not in event.metadata_payload
    return contact, event


def test_signed_business_greeting_uses_own_whatsapp_config_before_shared_and_records_without_classification(business_environment):
    env = business_environment
    _install_menu(env, _menu("Shared"), channel=None)
    _install_menu(env, _menu("Foreign"), tenant=env.other)
    _install_menu(env, _menu("Specific"))
    turn, replies, _ = _send(env, "Hola", "specific-greeting")
    assert "Specific synthetic welcome" in "\n".join(replies)
    assert "Specific service A" in "\n".join(replies)
    assert "Shared" not in "\n".join(replies) and "Foreign" not in "\n".join(replies)
    contact, _ = _assert_raw_event(env, turn, "Hola")
    assert contact.type == "unknown" and contact.name == "Contacto WhatsApp"
    assert Contact.query.count() == InteractionEvent.query.count() == 1
    assert not _state(turn)["active_inquiry"]


def test_signed_business_greeting_inherits_shared_only_when_specific_row_is_absent(business_environment):
    env = business_environment
    _install_menu(env, _menu("Shared"), channel=None)
    turn, replies, _ = _send(env, "Hola", "shared-greeting")
    assert "Shared synthetic welcome" in "\n".join(replies)
    assert "Shared service A" in "\n".join(replies)
    assert _state(turn)["displayed_options"]
    _assert_raw_event(env, turn, "Hola")


@pytest.mark.parametrize("value", [
    {}, [], "invalid", {"enabled": False, **{k: v for k, v in _menu().items() if k != "enabled"}},
    {"status": "disabled", **_menu()},
    {"enabled": "false", **{k: v for k, v in _menu().items() if k != "enabled"}},
    {"status": {}, **_menu()},
    {"enabled": True, "meta_native_text": {"contract_version": "tenant.meta_native_text.v1", "status": "configuration_required"}},
])
def test_specific_unusable_row_blocks_shared_inheritance(business_environment, value):
    env = business_environment
    _install_menu(env, _menu("Forbidden shared"), channel=None)
    _install_menu(env, value)
    turn, replies, _ = _send(env, "Hola", "blocked-specific")
    assert "Forbidden shared" not in "\n".join(replies)
    assert not _state(turn)["displayed_options"]
    _assert_raw_event(env, turn, "Hola")


def test_duplicate_shared_rows_do_not_select_an_arbitrary_configuration(business_environment):
    env = business_environment
    _install_menu(env, _menu("First"), channel=None)
    _install_menu(env, _menu("Second"), channel=None)
    turn, replies, _ = _send(env, "Hola", "ambiguous-shared")
    assert "First" not in "\n".join(replies) and "Second" not in "\n".join(replies)
    assert not _state(turn)["displayed_options"]


def test_foreign_menu_and_legacy_tenant_config_do_not_become_native_service_fallbacks(business_environment):
    env = business_environment
    _install_menu(env, _menu("Foreign only"), tenant=env.other)
    env.tenant.configuracion = {"menu": _menu("Legacy default")}
    db.session.commit()
    turn, replies, _ = _send(env, "Hola", "no-legacy-fallback")
    assert "Foreign only" not in "\n".join(replies) and "Legacy default" not in "\n".join(replies)
    assert not _state(turn)["displayed_options"]
    assert InteractionEvent.query.filter_by(tenant_id=env.other.id).count() == 0


@pytest.mark.parametrize("body", ["x", "a" * 4096, "  consulta sintética\ncon texto completo  "])
def test_free_text_without_config_persists_exact_claim_body_and_ack_without_inventing_a_menu(business_environment, body):
    env = business_environment
    turn, replies, _ = _send(env, body, "full-text")
    assert replies == [ACK]
    contact, _ = _assert_raw_event(env, turn, body)
    assert contact.type == "unknown" and len((contact.preferences or {}).get("last_message_excerpt", "")) <= 180
    state = _state(turn)
    assert state["active_inquiry"] is True and not state["displayed_options"]
    assert len(state["inquiry_event_ids"]) == 1


def test_long_text_and_own_contact_survive_existing_authenticated_crm_history_dto(business_environment):
    env = business_environment
    body = "a" * 1501
    turn, _, _ = _send(env, body, "dto-full-text")
    contact, _ = _assert_raw_event(env, turn, body)
    own, headers = _login(env, env.owner)
    base = f"/api/admin/tenants/{env.tenant.slug}/contacts"
    listing = _ok(own.get(base, headers=headers))
    assert [row["contact_id"] for row in listing["contacts"]] == [contact.id]
    dto = listing["contacts"][0]
    assert dto["contact_type"] == "unknown"
    assert dto["lead_temperature"] is dto["lead_score"] is None
    assert dto["marketing"] is dto["acepta_marketing"] is None
    history = _ok(own.get(f"{base}/{contact.id}/history", headers=headers))
    assert history["contact"]["id"] == contact.id and history["contact"]["phone"] == "+" + CONTACT
    assert len(history["interactions"]) == 1 and history["interactions"][0]["content"] == body
    assert len(history["interactions"][0]["content"]) > 1200
    foreign, foreign_headers = _login(env, env.other_owner)
    foreign_base = f"/api/admin/tenants/{env.other.slug}/contacts"
    assert foreign.get(f"{foreign_base}/{contact.id}/history", headers=foreign_headers).status_code == 404
    assert env.app.test_client().get(f"{base}/{contact.id}/history").status_code == 401


@pytest.mark.parametrize("declared_marketing", [True, False])
def test_foreign_namesake_and_existing_customer_preferences_do_not_create_new_identity_or_consent(business_environment, declared_marketing):
    env = business_environment
    own = Contact(id="00000000-0000-4000-8000-000000000101",
                  tenant_id=env.tenant.id, phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
                  name="Previously declared customer", type="customer",
                  preferences={"marketing_opt_in": declared_marketing, "lead_temperature": "hot", "lead_score": 85,
                               "last_intent": "previously-declared", "custom": "retained"})
    foreign = Contact(id="00000000-0000-4000-8000-000000000102",
                      tenant_id=env.other.id, phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
                      name="Foreign synthetic private contact", email="foreign-synthetic@example.invalid", type="customer")
    db.session.add_all([own, foreign])
    db.session.commit()
    own_id, foreign_id = own.id, foreign.id
    turn, replies, _ = _send(env, "quisiera saber más", "existing-customer")
    assert replies == [ACK]
    db.session.expire_all()
    saved = db.session.get(Contact, own_id)
    assert saved.name == "Previously declared customer" and saved.type == "customer"
    assert saved.preferences["marketing_opt_in"] is declared_marketing
    assert saved.preferences["lead_temperature"] == "hot" and saved.preferences["lead_score"] == 85
    assert saved.preferences["native_business_evidence_contract"] == EVIDENCE_CONTRACT
    assert saved.preferences["last_intent"] == "previously-declared" and saved.preferences["custom"] == "retained"
    serialized = crm.serialize_crm_contact(saved)
    assert serialized["lead_temperature"] == "hot" and serialized["lead_score"] == 85
    assert serialized["marketing"] is serialized["acepta_marketing"] is declared_marketing
    assert Contact.query.count() == 2 and db.session.get(Contact, foreign_id).name == "Foreign synthetic private contact"
    events = _own_events(env)
    assert len(events) == 1 and events[0].contact_id == own_id and events[0].content == "quisiera saber más"
    assert events[0].metadata_payload["durable_turn_id"] == turn.turn_id
    assert InteractionEvent.query.filter_by(tenant_id=env.other.id).count() == ContactSnapshot.query.count() == 0


def test_existing_contact_without_evidence_becomes_unknown_without_inventing_stored_facts_or_changing_unprocessed_legacy(business_environment):
    env = business_environment
    own = Contact(id="00000000-0000-4000-8000-000000000103",
                  tenant_id=env.tenant.id, phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
                  name="Existing synthetic contact", type="unknown", preferences={"custom": "retained"})
    legacy = Contact(id="00000000-0000-4000-8000-000000000104",
                     tenant_id=env.tenant.id, phone="+12025550144", whatsapp_id="+12025550144",
                     name="Unprocessed synthetic legacy contact", type="unknown", preferences={})
    db.session.add_all([own, legacy])
    db.session.commit()
    own_id, legacy_id = own.id, legacy.id
    _send(env, "consulta sin evidencia declarada", "existing-unknown")
    db.session.expire_all()
    saved = db.session.get(Contact, own_id)
    assert saved.preferences["custom"] == "retained"
    assert saved.preferences["native_business_evidence_contract"] == EVIDENCE_CONTRACT
    assert not {"lead_score", "lead_temperature", "marketing_opt_in"}.intersection(saved.preferences)
    dto = crm.serialize_crm_contact(saved)
    assert dto["lead_temperature"] is dto["lead_score"] is dto["marketing"] is dto["acepta_marketing"] is None
    untouched = db.session.get(Contact, legacy_id)
    assert untouched.preferences == {}
    legacy_dto = crm.serialize_crm_contact(untouched)
    assert legacy_dto["lead_temperature"] == "cold" and legacy_dto["lead_score"] == 0
    assert legacy_dto["marketing"] is legacy_dto["acepta_marketing"] is False
    assert Contact.query.count() == 2 and InteractionEvent.query.count() == 1 and ContactSnapshot.query.count() == 0


def test_second_text_reuses_contact_preserves_full_history_and_same_body_is_not_deduplicated_by_content(business_environment):
    env = business_environment
    first, _, _ = _send(env, "consulta igual", "first-consultation")
    first_contact, first_event = _assert_raw_event(env, first, "consulta igual")
    second, _, _ = _send(env, "consulta igual", "second-consultation")
    second_contact, second_event = _assert_raw_event(env, second, "consulta igual")
    assert second_contact.id == first_contact.id and second_event.id != first_event.id
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 2
    assert _state(second)["inquiry_event_ids"] == [first_event.id, second_event.id]


def test_signed_callback_replay_and_empty_drain_do_not_duplicate_crm_context_or_outbox(business_environment):
    env = business_environment
    turn, _, _ = _send(env, "consulta original", "replay")
    state = _state(turn)
    attempts = WhatsAppOutboundAttempt.query.count()
    assert _post(env, body="consulta original", message_id=turn.provider_message_sid) == turn.turn_id
    for _ in range(2):
        report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
        assert report["processed"] == report["completed"] == 0
    assert Contact.query.count() == InteractionEvent.query.count() == 1
    assert WhatsAppOutboundAttempt.query.count() == attempts and _state(turn) == state


def test_inquiry_reference_window_remains_bounded_and_every_reference_belongs_to_current_contact(business_environment):
    env = business_environment
    turn = None
    for index in range(21):
        turn, _, _ = _send(env, f"consulta sintética {index}", f"bounded-{index}")
    events = _own_events(env)
    state = _state(turn)
    assert len(events) == 21 and Contact.query.count() == 1
    assert state["inquiry_event_ids"] == [event.id for event in events[-20:]]
    assert all(event.tenant_id == env.tenant.id and event.contact_id == events[0].contact_id for event in events)


def test_real_writer_failure_rolls_back_contact_event_context_and_outbox_before_any_emit(business_environment, monkeypatch):
    env = business_environment
    turn_id = _post(env, body="consulta con fallo sintético", message_id="wamid.pyme/writer-failure+=")
    turn = _native_turn(turn_id)
    chat_id, inbound_id = turn.chat_session_id, turn.id
    context_before = deepcopy(db.session.get(ChatSessionContext, chat_id).context_data)
    original = crm.serialize_crm_contact
    calls, emitted = [], []

    def fail_after_real_writer_flush(contact, *args, **kwargs):
        original(contact, *args, **kwargs)
        assert Contact.query.count() == InteractionEvent.query.count() == 1
        assert ContactSnapshot.query.count() == 0 and emitted == []
        calls.append(contact.id)
        raise RuntimeError("synthetic business writer fault after flush")

    monkeypatch.setattr(crm, "serialize_crm_contact", fail_after_real_writer_flush)
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and len(calls) == 1, report
    assert report["results"][0]["error_code"] == "meta_pyme_crm_write_failed"
    db.session.expire_all()
    assert Contact.query.count() == InteractionEvent.query.count() == ContactSnapshot.query.count() == 0
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=inbound_id).count() == 0
    assert db.session.get(ChatSessionContext, chat_id).context_data == context_before and emitted == []


def test_finalizer_failure_keeps_crm_state_and_reply_atomic_without_before_commit_realtime(business_environment, monkeypatch):
    env = business_environment
    turn_id = _post(env, body="consulta antes del commit", message_id="wamid.pyme/finalizer-failure+=")
    turn = _native_turn(turn_id)
    chat_id, inbound_id = turn.chat_session_id, turn.id
    context_before = deepcopy(db.session.get(ChatSessionContext, chat_id).context_data)
    reached, emitted = [], []

    def fail_before_commit(unit, *args, **kwargs):
        assert Contact.query.count() == InteractionEvent.query.count() == 1 and emitted == []
        reached.append(unit.claim.turn_id)
        unit.fail("synthetic_pyme_finalizer_failure")

    monkeypatch.setattr(ConversationUnitOfWork, "commit_reply", fail_before_commit)
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and reached == [turn_id], report
    db.session.expire_all()
    assert Contact.query.count() == InteractionEvent.query.count() == ContactSnapshot.query.count() == 0
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=inbound_id).count() == 0
    assert db.session.get(ChatSessionContext, chat_id).context_data == context_before and emitted == []


def test_only_a_previously_displayed_current_service_can_return_configured_details(business_environment):
    env = business_environment
    _install_menu(env, _menu())
    greeting, _, _ = _send(env, "Hola", "display-service")
    assert _state(greeting)["displayed_options"] == [{"id": "service-a", "texto": "Specific service A"}]
    selected, replies, _ = _send(env, "1", "select-service")
    assert replies == ["Specific service A\n\nSpecific synthetic service description"]
    assert _state(selected)["selected_service_id"] == "service-a"
    assert _state(selected)["active_inquiry"] is False
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 2


def test_a_numeric_message_without_displayed_options_is_full_text_inquiry_not_service_selection(business_environment):
    env = business_environment
    _install_menu(env, _menu())
    turn, replies, _ = _send(env, "1", "no-selection-authority")
    assert replies == [ACK]
    state = _state(turn)
    assert state["active_inquiry"] is True and state["selected_service_id"] is None
    _assert_raw_event(env, turn, "1")


@pytest.mark.parametrize("change", ["revision", "source"])
def test_old_displayed_options_cannot_select_after_revision_or_channel_source_changes(business_environment, change):
    env = business_environment
    menu = _install_menu(env, _menu("Old"), channel=None if change == "source" else "whatsapp")
    greeting, _, _ = _send(env, "Hola", "before-menu-change")
    before = _state(greeting)
    if change == "revision":
        menu.json_value = _menu("New")
        db.session.commit()
    else:
        _install_menu(env, _menu("New"))
    selected, replies, _ = _send(env, "1", "stale-menu-selection")
    assert replies == ["New synthetic service unavailable"]
    state = _state(selected)
    assert state["selected_service_id"] is None and state["active_inquiry"] == before["active_inquiry"]
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 2
    assert "description" not in "\n".join(replies)


def test_greeting_preserves_an_active_inquiry_instead_of_resetting_its_context(business_environment):
    env = business_environment
    first, _, _ = _send(env, "consulta ya expresada", "inquiry-before-greeting")
    before = _state(first)
    greeting, _, _ = _send(env, "Hola", "greeting-during-inquiry")
    state = _state(greeting)
    assert before["active_inquiry"] is state["active_inquiry"] is True
    assert state["inquiry_event_ids"][:-1] == before["inquiry_event_ids"]
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 2


@pytest.mark.parametrize("patch", [
    {CONTEXTO_MUNICIPIO: {SURVEY_NAVIGATION_KEY: {}}},
    {CONTEXTO_MUNICIPIO: {WHATSAPP_SURVEY_FLOW_STATE_KEY: {}}},
    {"pending_sensitive_action": {"synthetic": True}},
    {"human_chat_in_progress": True},
    {"room": "synthetic-human-room"},
    {"education_context": {"synthetic": True}},
    {CONTEXT_KEY: {"estado_conversacion": "WAITING_FOR_EXISTING_FLOW"}},
    {CONTEXT_KEY: {"live_chat_ticket_id": 991}},
    {CONTEXT_KEY: {"multimodal_state_v1": {"cart": {"items": [{"id": "synthetic-item"}]}}}},
    {CONTEXT_KEY: {"multimodal_state_v1": {"delivery_location_pending": True}}},
])
def test_unported_or_sensitive_existing_flow_is_held_without_crm_or_context_reset(business_environment, monkeypatch, patch):
    env = business_environment
    turn_id = _post(env, body="Hola", message_id="wamid.pyme/unported-flow+=")
    turn = _native_turn(turn_id)
    chat_id, inbound_id = turn.chat_session_id, turn.id
    context = db.session.get(ChatSessionContext, chat_id)
    data = deepcopy(context.context_data)
    data.update(deepcopy(patch))
    context.context_data = data
    db.session.commit()
    context_before = deepcopy(data)
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and report["results"][0]["error_code"] == "meta_pyme_flow_not_ported", report
    db.session.expire_all()
    assert db.session.get(ChatSessionContext, chat_id).context_data == context_before
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=inbound_id).count() == 0 and emitted == []


@pytest.mark.parametrize("foreign_tenant", [True, False])
def test_context_cannot_reference_another_tenant_or_contact_interaction(business_environment, foreign_tenant):
    env = business_environment
    first, _, _ = _send(env, "consulta propia", "before-forged-reference")
    owner = env.other if foreign_tenant else env.tenant
    contact = Contact(id="00000000-0000-4000-8000-000000000105",
                      tenant_id=owner.id, phone="+12025550144", whatsapp_id="+12025550144",
                      name="Other synthetic contact", type="unknown")
    db.session.add(contact)
    db.session.flush()
    event = InteractionEvent(tenant_id=owner.id, contact_id=contact.id, channel="whatsapp", direction="inbound",
                             content_type="text", content="Other synthetic content", metadata_payload={})
    db.session.add(event)
    db.session.flush()
    context = db.session.get(ChatSessionContext, first.chat_session_id)
    data = deepcopy(context.context_data)
    data[CONTEXT_KEY][STATE_KEY]["inquiry_event_ids"] = [event.id]
    context.context_data = data
    db.session.commit()
    turn_id = _post(env, body="siguiente consulta", message_id="wamid.pyme/foreign-reference+=")
    turn = _native_turn(turn_id)
    context_before = deepcopy(db.session.get(ChatSessionContext, turn.chat_session_id).context_data)
    event_count, contact_count = InteractionEvent.query.count(), Contact.query.count()
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and report["results"][0]["error_code"] == "meta_pyme_event_reference_invalid", report
    db.session.expire_all()
    assert InteractionEvent.query.count() == event_count and Contact.query.count() == contact_count
    assert db.session.get(ChatSessionContext, turn.chat_session_id).context_data == context_before
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=turn.id).count() == 0


def test_non_text_input_is_not_reinterpreted_as_an_inquiry(business_environment):
    env = business_environment
    turn_id = _post(env, kind="location", message_id="wamid.pyme/unsupported-location+=")
    turn = _native_turn(turn_id)
    context_before = deepcopy(db.session.get(ChatSessionContext, turn.chat_session_id).context_data)
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and report["results"][0]["error_code"] == "meta_pyme_kind_unsupported", report
    db.session.expire_all()
    assert Contact.query.count() == ContactSnapshot.query.count() == InteractionEvent.query.count() == 0
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=turn.id).count() == 0
    assert db.session.get(ChatSessionContext, turn.chat_session_id).context_data == context_before


def test_configured_initial_and_continuation_ack_use_distinct_backend_copy_without_repeating_menu(business_environment):
    env = business_environment
    menu = _menu()
    menu["meta_native_text"]["messages"]["inquiry_received"] = "Synthetic initial receipt from backend"
    menu["meta_native_text"]["messages"]["inquiry_continued"] = "Synthetic continuation receipt from backend"
    _install_menu(env, menu)
    first, first_replies, _ = _send(env, "consulta inicial completa", "configured-first-ack")
    second, second_replies, _ = _send(env, "más contexto sin repetir el menú", "configured-continuation-ack")
    assert first_replies == ["Synthetic initial receipt from backend"]
    assert second_replies == ["Synthetic continuation receipt from backend"]
    assert _state(second)["active_inquiry"] is True
    assert _state(second)["displayed_options"] == []
    contact, first_event = _assert_raw_event(env, first, "consulta inicial completa")
    second_contact, second_event = _assert_raw_event(env, second, "más contexto sin repetir el menú")
    assert contact.id == second_contact.id and Contact.query.count() == 1
    assert _state(second)["inquiry_event_ids"] == [first_event.id, second_event.id]


@pytest.mark.parametrize("change", ["revision", "source", "disabled"])
def test_greeting_retires_stale_service_selection_but_preserves_existing_inquiry_and_references(business_environment, change):
    env = business_environment
    row = _install_menu(env, _menu("Old"), channel=None if change == "source" else "whatsapp")
    _send(env, "Hola", "selection-before-refresh-greeting")
    _send(env, "consulta ya expresada", "inquiry-before-refresh-greeting")
    selected, _, _ = _send(env, "1", "service-before-refresh-greeting")
    before = _state(selected)
    assert before["selected_service_id"] == "service-a" and before["active_inquiry"] is True
    if change == "source":
        _install_menu(env, _menu("New"))
    else:
        value = _menu("New")
        if change == "disabled":
            value["enabled"] = False
        row.json_value = value
        db.session.commit()
    greeting, replies, _ = _send(env, "Hola", "greeting-with-refreshed-source")
    after = _state(greeting)
    assert after["selected_service_id"] is None and after["active_inquiry"] is True
    assert after["inquiry_event_ids"][:-1] == before["inquiry_event_ids"]
    assert Contact.query.count() == 1 and InteractionEvent.query.count() == 4
    if change == "disabled":
        assert after["displayed_options"] == [] and after["menu_revision_sha256"] is None
        assert "New" not in "\n".join(replies)
    else:
        assert after["displayed_options"] == [{"id": "service-a", "texto": "New service A"}]
        assert after["menu_revision_sha256"] != before["menu_revision_sha256"]
        assert "New synthetic welcome" in "\n".join(replies)


def test_persisted_event_without_finalized_reply_requires_reconciliation_instead_of_completing_empty(business_environment, monkeypatch):
    env = business_environment
    body = "consulta con efecto previo sintético"
    turn_id = _post(env, body=body, message_id="wamid.pyme/persisted-effect-pending-claim+=")
    turn = _native_turn(turn_id)
    assert turn.status == "received"
    inbound_id, chat_id = turn.id, turn.chat_session_id
    contact = Contact(id="00000000-0000-4000-8000-000000000106",
                      tenant_id=env.tenant.id, phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
                      name="Contacto WhatsApp", type="unknown",
                      preferences={"native_business_evidence_contract": EVIDENCE_CONTRACT})
    db.session.add(contact)
    db.session.flush()
    effect_key = f"whatsapp:{env.tenant.id}:{turn.turn_id}:crm.native_business_inbound"
    event = InteractionEvent(tenant_id=env.tenant.id, contact_id=contact.id,
        channel="whatsapp", direction="inbound", content_type="text", content=body,
        metadata_payload={"event_type": "whatsapp_inbound", "source": "whatsapp_webhook", "provider": "meta",
            "message_sid": turn.provider_message_sid, "source_event_id": turn.provider_message_sid,
            "durable_turn_id": turn.turn_id, "idempotency_key": effect_key,
            "native_business_contract": "pyme.meta_native_text.v1"})
    db.session.add(event)
    db.session.commit()
    contact_id, event_id = contact.id, event.id
    context_before = deepcopy(db.session.get(ChatSessionContext, chat_id).context_data)
    emitted = []
    monkeypatch.setattr(crm, "emit_crm_contact_update", lambda *_args: emitted.append(True))
    report = worker.process_whatsapp_inbound_stream(tenant_id=env.tenant.id, limit=1)
    assert report["completed"] == 0 and report["results"][0]["error_code"] == "meta_pyme_crm_reconciliation_required", report
    db.session.expire_all()
    assert _native_turn(turn_id).status != "completed"
    assert Contact.query.count() == InteractionEvent.query.count() == 1
    assert db.session.get(Contact, contact_id).preferences == {"native_business_evidence_contract": EVIDENCE_CONTRACT}
    assert db.session.get(InteractionEvent, event_id).content == body
    assert ContactSnapshot.query.count() == 0 and emitted == []
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=inbound_id).count() == 0
    assert db.session.get(ChatSessionContext, chat_id).context_data == context_before


@pytest.mark.parametrize(("stored_score", "expected_score"), [
    (10 ** 1000, None), (-(10 ** 1000), None), (0, 0), (85, 85), (87.5, 87.5),
], ids=["too-large-positive", "too-large-negative", "explicit-zero", "finite-integer", "finite-fractional"])
def test_stored_score_projection_handles_extreme_integers_without_dropping_the_crm_dto_or_rewriting_evidence(business_environment, stored_score, expected_score):
    env = business_environment
    existing = Contact(id="00000000-0000-4000-8000-000000000107",
                       tenant_id=env.tenant.id, phone="+" + CONTACT, whatsapp_id="+" + CONTACT,
                       name="Existing synthetic score evidence", type="unknown",
                       preferences={"lead_score": stored_score})
    db.session.add(existing)
    db.session.commit()
    contact_id = existing.id
    _send(env, "consulta con evidencia almacenada", "stored-score-projection")
    db.session.expire_all()
    saved = db.session.get(Contact, contact_id)
    assert saved.preferences["lead_score"] == stored_score
    assert saved.preferences["native_business_evidence_contract"] == EVIDENCE_CONTRACT
    assert crm.serialize_crm_contact(saved)["lead_score"] == expected_score
    owner, headers = _login(env, env.owner)
    listing = _ok(owner.get(f"/api/admin/tenants/{env.tenant.slug}/contacts", headers=headers))
    assert len(listing["contacts"]) == 1 and listing["contacts"][0]["contact_id"] == contact_id
    assert listing["contacts"][0]["lead_score"] == expected_score
    assert listing["contacts"][0]["lead_temperature"] is None
    assert listing["contacts"][0]["marketing"] is None
    assert Contact.query.count() == InteractionEvent.query.count() == 1 and ContactSnapshot.query.count() == 0
