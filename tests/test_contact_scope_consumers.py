"""Proposed new consumer cases; local ORM and real consumers, never provider acceptance."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from models import ChatSessionContext, ProviderSender, User, db
from models_memory import Contact
from services.contact_service import read_contact_projection, resolve_contact
from sqlalchemy import event


@pytest.mark.parametrize("kind", ["Contact-empty", "Contact-sanitized", "User-empty", "User-sanitized"])
def test_known_record_without_usable_name_preserves_own_email_with_channel_profile(contact_env, kind):
    e = contact_env
    item = e.add_contact("b") if kind.startswith("Contact") else e.b.user
    item.name = "" if kind.endswith("empty") else "hola"
    db.session.commit()
    projection = resolve_contact(e.phone, "Synthetic channel display", tenant=e.b.tenant)
    assert projection["nombre"] is None
    assert projection["email"] == item.email != e.a.user.email
    assert projection["is_known_contact"] is True
    assert read_contact_projection(projection, tenant_id=e.b.tenant.id, phone=e.phone) == projection


def test_expired_tenant_primary_key_does_not_flush_pending_caller_data(contact_env):
    e = contact_env
    prior_name = e.a.user.name
    prior_id = e.a.user.id
    expected_email = e.b.user.email
    e.a.user.name = "Synthetic pending caller name"
    db.session.expire(e.b.tenant)
    statements = []
    def observe(*args):
        statements.append(args[2])
    event.listen(db.engine, "before_cursor_execute", observe)
    try:
        result = resolve_contact(e.phone, None, tenant=e.b.tenant)
        assert result["email"] == expected_email
        assert e.a.user.name == "Synthetic pending caller name"
        assert e.a.user in db.session.dirty
        assert all(not sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for sql in statements)
        with db.session.no_autoflush:
            assert db.session.query(User.name).filter(User.id == prior_id).scalar() == prior_name
    finally:
        event.remove(db.engine, "before_cursor_execute", observe)


@pytest.mark.parametrize("kind", ["Contact", "User", "Contact-empty-profile"])
def test_finisher_keeps_own_data_and_raw_profile_separate_from_lookup(contact_env, kind):
    from services.whatsapp_conversation_contract import (
        CollectingConversationSink, ConversationMode, ConversationSelection, ConversationTurn,
    )
    from services.whatsapp_conversation_turn import finish_conversation_turn
    from services import contact_service
    from services.llm_utils import extract_multiple_contact_details_llm
    from services.media_classifier import clasificar_adjunto_whatsapp
    from routes import whatsapp_webhook as route

    e = contact_env
    item = e.add_contact("b") if kind.startswith("Contact") else e.b.user
    raw_profile = "Synthetic channel display" if kind.endswith("profile") else None
    if kind.endswith("profile"):
        item.name = "hola"
        db.session.commit()
    explicit = {"nombre": "Explicit draft name", "email": "explicit@example.invalid"}
    e.session.context_data = {"synthetic_explicit_draft": deepcopy(explicit)}
    db.session.commit()
    captures = []

    def engine(**kwargs):
        captures.append({"owner": kwargs["owner_user"].id, "user": kwargs["current_user"].id,
            "tenant": kwargs["tenant_profile"].id, "profile": kwargs.get("profile_name"),
            "projection": deepcopy(kwargs.get("resolved_contact")),
            "cache": deepcopy(kwargs["chat_db_context"].context_data.get("contact_cache"))})
        return {"message_body": "Synthetic scoped response.", "options_list": [],
            "message_type": "text", "fuente": "synthetic_engine", "skip_audio_generation": True}

    turn = ConversationTurn(owner_user=e.b.owner, tenant_profile=e.b.tenant,
        tenant_id=e.b.tenant.id, end_user=e.b.user, session_context=e.session,
        chat_session_id=e.session.chat_session_id, contact_identity=e.phone,
        message_body="Consulta sintética de B", original_text="Consulta sintética de B",
        action_id=None, source_event_id="synthetic-new-scoped-event", durable_claim=None,
        mode=ConversationMode(False, False), original_profile_name=raw_profile, profile_name=raw_profile)
    sink = CollectingConversationSink(supports_media=False)
    query_counts = []
    real_core = contact_service._resolve_scoped_contact
    def bounded_core(*args, **kwargs):
        statements = []
        def observe(*event_args):
            statements.append(event_args[2])
        event.listen(db.engine, "before_cursor_execute", observe)
        try:
            return real_core(*args, **kwargs)
        finally:
            event.remove(db.engine, "before_cursor_execute", observe)
            query_counts.append(len(statements))
    # Observer delegates to the real query implementation; it fabricates no data.
    with patch.object(contact_service, "_resolve_scoped_contact", side_effect=bounded_core):
        result = finish_conversation_turn(turn, ConversationSelection(turn.message_body, None, None, None), sink,
            prepared_response=None, public_base_url="https://scoped.example.invalid", engine=engine,
            classify_attachment=clasificar_adjunto_whatsapp,
            extract_contact_details=extract_multiple_contact_details_llm,
            resolve_input_contact=lambda phone, name: resolve_contact(phone, name, tenant=e.b.tenant),
            receipt_header=lambda: None, normalize_flow_contract=route._normalize_whatsapp_flow_contract,
            on_failure=lambda code, exc: route._durable_replay_failure_or_retry(False, code, exc),
            replay_error=route.WhatsAppDurableReplayError, log=route._log,
            safe_reference=route._safe_provider_reference, safe_context_metadata=route._safe_session_context_metadata)
    assert len(query_counts) == 2 and all(1 <= count <= 5 for count in query_counts)
    assert result.legacy_return is None and len(sink.snapshot()) == len(captures) == 1
    seen = captures[0]
    assert (seen["owner"], seen["user"], seen["tenant"]) == (e.b.owner.id, e.b.user.id, e.b.tenant.id)
    assert seen["projection"]["email"] == item.email != e.a.user.email
    assert seen["cache"] == seen["projection"]
    assert seen["profile"] == raw_profile  # DB name never becomes a channel declaration.
    assert e.session.context_data["synthetic_explicit_draft"] == explicit
    assert e.session.context_data.get("profile_name") is None


class _NoTransportSocket:
    def send(self, *_args, **_kwargs):
        raise AssertionError("unexpected_transport")


@pytest.mark.parametrize("case", ["valid", "wrong-phone", "ambiguous-phone", "renamed", "revoked", "explicit"])
def test_voice_cached_identity_revalidates_without_erasing_explicit_draft(contact_env, case):
    from services.voice_stream_service import VoiceStreamService
    e = contact_env
    item = e.add_contact("b")
    old_name = item.name
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    service = VoiceStreamService(_NoTransportSocket(), app=e.app)
    service.tenant_profile, service.user, service.from_number = e.b.tenant, e.b.user, e.phone
    context = e.cache_context(projection, explicit=case == "explicit")
    preserved = deepcopy(context.get("contexto_municipio_v2"))
    service.context_data_snapshot = context
    if case == "wrong-phone":
        service.from_number = e.other_phone
        service.user = SimpleNamespace(telefono=e.other_phone, name="Synthetic current user")
    elif case == "ambiguous-phone":
        service.from_number = e.other_phone
    elif case == "renamed":
        item.name = "Synthetic renamed canonical"
        db.session.commit()
    elif case == "revoked":
        item.tenant_id = e.a.tenant.id
        db.session.commit()
    identity = service._resolve_identity_from_context(context)
    if case == "valid":
        assert identity["nombre"] == old_name
        assert service._resolve_greeting_name() == old_name
    elif case == "explicit":
        assert identity["nombre"] == "Explicit draft B"
    else:
        assert identity["nombre"] is None
        assert service._resolve_greeting_name() != old_name
    assert context.get("contexto_municipio_v2") == preserved


@pytest.mark.parametrize("explicit", [False, True])
def test_voice_context_does_not_promote_lookup_name_to_user_or_profile(contact_env, explicit):
    from services.voice_stream_service import VoiceStreamService
    e = contact_env
    e.b.user.name = "Vecino"
    item = e.add_contact("b")
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    e.session.context_data = e.cache_context(projection, explicit=explicit)
    bot = "+15550000203"
    e.b.tenant.configuracion = {"twilio_voice_number": bot}
    db.session.commit()
    service = VoiceStreamService(_NoTransportSocket(), app=e.app)
    service.from_number = e.phone
    service.source_chat_session_id = e.session.chat_session_id
    assert service._resolve_context(e.phone, bot, "CAsynthetic-scoped-context") is True
    assert service.tenant_profile.id == e.b.tenant.id and service.user.id == e.b.user.id
    assert service._resolve_identity_from_context(service.context_data_snapshot)["nombre"] == (
        "Explicit draft B" if explicit else item.name)
    assert service.user.name == ("Explicit draft B" if explicit else "Vecino")
    row = ChatSessionContext.query.filter_by(chat_session_id=service.chat_session_id).one()
    assert row.context_data.get("profile_name") == ("Explicit synthetic B" if explicit else None)
    assert row.context_data["resolved_contact"]["email"] == item.email
    if not explicit:
        item.tenant_id = e.a.tenant.id
        db.session.commit()
        assert service._resolve_identity_from_context(row.context_data)["nombre"] is None
        assert service._resolve_greeting_name() != item.name


@pytest.mark.parametrize("case", ["valid", "ambiguous"])
def test_voice_claim_passes_only_current_scoped_projection_to_action(contact_env, case):
    from services.voice_stream_service import VoiceStreamService
    e = contact_env
    item = e.add_contact("b")
    service = VoiceStreamService(_NoTransportSocket(), app=e.app)
    service.openai_ws = _NoTransportSocket()
    service.tenant_profile, service.owner_user, service.user = e.b.tenant, e.b.owner, e.b.user
    service.from_number = e.phone if case == "valid" else e.other_phone
    service.chat_session_id = e.session.chat_session_id
    with patch("services.actions.municipio_actions.CrearReclamoActionHandler") as handler, \
         patch.object(service, "_claim_realtime_tool_call", return_value=("execute", None)), \
         patch.object(service, "_complete_realtime_tool_call", side_effect=lambda *a, **k: str(k["output"])), \
         patch.object(service, "_send_tool_result"):
        handler.return_value.execute.return_value = {"success": False, "message_body": "Faltan datos."}
        service.execute_tool("synthetic-scoped-claim", "crear_reclamo", json.dumps({"descripcion": "Poste caído"}))
    projection = handler.call_args.args[0]["resolved_contact"]
    assert projection == ({} if case == "ambiguous" else resolve_contact(e.phone, None, tenant=e.b.tenant))
    if case == "valid":
        assert projection["email"] == item.email


class _StopAfterContactRead(BaseException):
    pass


@pytest.mark.parametrize("case", ["valid", "wrong-phone", "revoked", "explicit"])
def test_claim_reader_uses_server_phone_before_prefill_and_preserves_existing_draft(contact_env, case):
    from services.actions import municipio_actions as actions
    e = contact_env
    item = e.add_contact("b")
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    draft = {"nombre": "Explicit action name", "email": "explicit-action@example.invalid"} if case == "explicit" else {}
    if case == "revoked":
        item.tenant_id = e.a.tenant.id
        db.session.commit()
    context = {"tenant_id": e.b.tenant.id, "anon_id": e.phone if case != "wrong-phone" else e.other_phone,
        "viewer_user_obj": e.b.user, "user_obj": e.b.owner, "channel": "whatsapp",
        "resolved_contact": projection, "contexto_municipio_v2": {"contacto_usuario": draft}}
    # Stop after the real reader and prefill; this is not ticket creation or category authorization acceptance.
    with patch.object(actions, "_build_reclamo_callback_request_metadata", side_effect=_StopAfterContactRead):
        with pytest.raises(_StopAfterContactRead):
            actions.CrearReclamoActionHandler(context).execute({})
    if case == "explicit":
        assert draft["nombre"] == "Explicit action name" and draft["email"] == "explicit-action@example.invalid"
    elif case == "valid":
        assert draft["email"] == item.email
    else:
        assert not draft


@pytest.mark.parametrize("case", ["valid", "wrong-phone", "missing-phone", "revoked", "explicit"])
def test_survey_reader_revalidates_with_current_server_phone_not_declared_draft(contact_env, case):
    from services import whatsapp_survey_conversation as survey
    e = contact_env
    item = e.add_contact("b")
    projection = resolve_contact(e.phone, None, tenant=e.b.tenant)
    if case == "revoked":
        item.tenant_id = e.a.tenant.id
        db.session.commit()
    viewer = e.b.user if case != "missing-phone" else SimpleNamespace(telefono=None)
    anon = e.phone if case not in {"wrong-phone", "missing-phone"} else (e.other_phone if case == "wrong-phone" else "opaque-session")
    context = {"tenant_id": e.b.tenant.id, "anon_id": anon, "viewer_user_obj": viewer,
        "resolved_contact": projection}
    if case == "explicit":
        context["phone"] = e.other_phone
    captured = []
    def observing_reader(value, **kwargs):
        result = read_contact_projection(value, **kwargs)
        captured.append((kwargs, deepcopy(result)))
        return result
    with patch.object(survey, "read_contact_projection", side_effect=observing_reader):
        result = survey._participant_phone(context)
    assert len(captured) == 1
    kwargs, projection_used = captured[0]
    assert kwargs["tenant_id"] == e.b.tenant.id
    if case in {"wrong-phone", "missing-phone"}:
        assert kwargs["phone"] is None
    if case in {"valid", "explicit"}:
        assert projection_used["email"] == item.email
    else:
        assert projection_used == {}
    if case == "explicit":
        assert result == e.other_phone


def test_repeated_real_welcome_does_not_launder_lookup_into_declared_name():
    from tests import test_whatsapp_welcome_each_greeting as welcome_fixture
    from routes import whatsapp_webhook as route
    fixture = welcome_fixture.JuniWelcomeEachGreetingTests("runTest")
    fixture.setUp()
    try:
        # The inherited +1 fixture number is not canonical under the existing
        # Argentina-default normalizer. Bind this new synthetic scenario to a
        # canonical +54 identity, including its already-confirmed session.
        old_phone = fixture.fixture.test_user_number_str
        phone = "+5491100000203"
        owner = fixture.fixture.mock_client_user
        session = ChatSessionContext.query.filter_by(anon_id=old_phone).one()
        fixture.fixture.test_user_number_str = phone
        session.anon_id = phone
        session.chat_session_id = f"whatsapp_{owner.id}_{phone}"
        context = deepcopy(session.context_data)
        context["telefono_usuario"] = phone
        session.context_data = context
        person = User(name="Vecino", email="synthetic-welcome@example.invalid", telefono=phone,
            tenant_id=fixture.tenant.id, tenant_slug=fixture.tenant.slug,
            empresa_id=owner.id, municipio_id=owner.id, tipo_chat="municipio", rol="usuario",
            password_hash="synthetic-not-a-login")
        item = Contact(id=str(uuid4()), tenant_id=fixture.tenant.id, name="Synthetic welcome lookup",
            phone=phone, whatsapp_id=phone, email="synthetic-welcome-canonical@example.invalid")
        db.session.add_all([person, item]); db.session.commit()
        initial = resolve_contact(phone, None, tenant=fixture.tenant)
        assert initial["telefono"] == phone
        assert initial["nombre"] == item.name and initial["email"] == item.email
        calls = fixture.post(93001, profile=False)
        fixture.assert_welcome(calls)
        assert item.name in " ".join(call.get("body", "") for call in calls)
        context = fixture.context()
        assert not context.get("profile_name")
        assert not context.get("contexto_municipio_v2", {}).get("contacto_usuario", {}).get("nombre")
        old_name = item.name
        item.phone = item.whatsapp_id = "+15550000999"
        db.session.commit()
        calls = fixture.post(93002, offset=1, profile=False)
        fixture.assert_welcome(calls)
        assert old_name not in " ".join(call.get("body", "") for call in calls)
        # Explicit name capture remains a declaration and is never deleted by equality.
        route._remember_contact_name(fixture.session(), "Explicit welcome declaration")
        db.session.commit()
        assert fixture.context()["profile_name"] == "Explicit welcome declaration"
    finally:
        fixture.doCleanups()
        fixture.fixture.doCleanups()  # The manually composed inner TestCase owns env cleanup too.
