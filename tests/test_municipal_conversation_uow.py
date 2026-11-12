"""PREP only: real municipal flow/actions/ORM/UoW, synthetic input and sockets.

No old 72/54/writer cohort is selected here. The runner must call
prepare_process before imports; no fixture installs a production configuration.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from models import (
    CategoriaTicket, MunicipioTicket, TicketComentario, TicketDomainEffectReceipt,
    DomainEffectOutbox, User, WhatsAppOutboundAttempt, db,
)
from services import municipio_responder as municipal
from services.actions import municipio_actions as actions
from services import municipal_conversation_uow as boundary
from services import meta_whatsapp_webhook as webhook
from services.meta_whatsapp_runtime import APP_SECRETS_KEY
from services.meta_whatsapp_outbox import MetaReplyBundle, MetaReplyContent
from services.whatsapp_conversation_uow import ConversationUnitOfWorkError
from services.constants import CONTEXTO_MUNICIPIO
from tests.test_whatsapp_conversation_uow import environment, _pending_claim, _unit


def _physical(env):
    env.actual_session = db.session()
    driver = env.actual_session.connection().connection.driver_connection
    if not driver.in_transaction:
        env.actual_session.execute(text("BEGIN"))


@pytest.fixture
def municipal_env(environment, monkeypatch):
    env = environment
    # The Meta app/WABA/sender/tenant fixture remains the same verified synthetic
    # binding. Business kind changes before claim/ownership, not in a Unit.
    env.tenant.tipo, env.tenant.pyme_id, env.tenant.municipio_id = "municipio", None, env.owner.id
    env.owner.tipo_chat, env.owner.municipio_id = "municipio", env.owner.id
    category = CategoriaTicket(tenant_id=env.tenant.id, nombre="Luminaria", tipo="ticket")
    db.session.add(category)
    config = {"nombre": "Synthetic municipality", "ciudad": "Synthetic city",
        "provincia": "Synthetic province", "base_chat_url": "https://example.invalid/chat",
        "campos_requeridos_reclamo": ["descripcion", "ubicacion", "nombre", "telefono", "email"],
        "closing_promo_enabled": False}
    env.tenant.configuracion = dict(config)
    env.app.config.update(SECRET_KEY="synthetic-municipal-uow-secret-" + "x" * 32,
        DOMAIN_EFFECT_OUTBOX_MODE="queue", DOMAIN_EFFECT_OUTBOX_SECRET="synthetic-domain-" + "x" * 32,
        DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(env.tenant.id))
    db.session.commit()
    # Public configuration I/O is the declared seam; category/tenant/model
    # authority, address validation, flow, writers and serializer remain real.
    monkeypatch.setattr(municipal, "CONFIG_MUNICIPIO", dict(config))
    monkeypatch.setattr(municipal, "cargar_configuracion_municipio", lambda *_a, **_k: {})
    monkeypatch.setattr(actions, "cargar_configuracion_municipio", lambda *_a, **_k: {})
    monkeypatch.setattr(actions.servicio_tickets, "auto_assign_enabled", False)
    source = env.event.sender
    document = {"object": "whatsapp_business_account", "entry": [{"id": source.waba_id,
        "changes": [{"field": "messages", "value": {"messaging_product": "whatsapp",
            "metadata": {"phone_number_id": source.phone_number_id}, "messages": [{
                "from": env.event.contact, "id": env.event.message_id,
                "timestamp": str(env.now - 10), "type": "text", "text": {"body": "Sí, confirmo"}}]}}]}]}
    raw = json.dumps(document, separators=(",", ":")).encode()
    secret = env.app.config[APP_SECRETS_KEY][source.app_id]
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    env.event = webhook.parse_webhook(raw_body=raw, signature=signature,
        app_secret=secret, app_id=source.app_id, now=env.now,
        binding_resolver=lambda phone, app_id, waba: env.runtime.load_binding(source)
            if (phone, app_id, waba) == (source.phone_number_id, source.app_id, source.waba_id)
            else (_ for _ in ()).throw(AssertionError("foreign synthetic binding")))[0]
    env.claim = _pending_claim(env)
    from models import ChatSessionContext
    env.context = ChatSessionContext.query.filter_by(chat_session_id=env.claim.chat_session_id).one()
    env.context.context_data = {CONTEXTO_MUNICIPIO: {}}
    db.session.commit()
    _physical(env)
    env.category, env.municipal_config = category, config
    env.sockets = []
    monkeypatch.setattr(boundary, "_emit_ticket", lambda payload: env.sockets.append(("ticket", payload)))
    monkeypatch.setattr(boundary, "_emit_live_request", lambda payload, room: env.sockets.append(("live", room)))
    return env


def _context(env):
    return {"user_obj": env.owner, "viewer_user_obj": None, "cliente_id": None,
        "tenant_profile": env.tenant, "tenant_id": env.tenant.id,
        "municipio_id": env.owner.id, "anon_id": "+" + env.event.contact, "channel": "whatsapp",
        "chat_db_context_obj": env.context, "chat_db_context_data": env.context.context_data,
        "chat_session_uuid": env.claim.chat_session_id, "source_event_id": env.claim.provider_message_sid,
        "durable_turn_id": env.claim.turn_id, "user_input_raw": env.event.text,
        "pregunta_actual_usuario": env.event.text, "municipio_config_actual": env.municipal_config,
        CONTEXTO_MUNICIPIO: env.context.context_data[CONTEXTO_MUNICIPIO]}


def _args(env, unit):
    return {"unit_of_work": unit, "conversation_turn_id": env.claim.turn_id}


def _responder(env, unit, pregunta=None, **changes):
    args = {"tenant_profile": env.tenant, "tenant_id": env.tenant.id,
        "source_event_id": env.claim.provider_message_sid, "durable_turn_id": env.claim.turn_id,
        "chat_session_uuid": env.claim.chat_session_id, **_args(env, unit)}
    args.update(changes)
    return municipal.responder_municipio(env.event.text if pregunta is None else pregunta,
        owner_user=env.owner, rubro_obj=None, viewer_user=None, chat_db_context=env.context,
        anon_id="+" + env.event.contact, channel="whatsapp", **args)


def _claim_data(env):
    return {"categoria": env.category.nombre, "descripcion": "Synthetic street light fault",
        "direccion": "Calle Sintética 123, Distrito Centro", "nombre": "Synthetic citizen",
        "dni": "30111222", "email": "citizen@example.invalid", "telefono": "+" + env.event.contact}


def _draft(env):
    data = _claim_data(env)
    env.context.context_data = {CONTEXTO_MUNICIPIO: {"estado_conversacion": "EN_FLUJO_RECLAMO",
        "reclamo_flow_v2": {"state": municipal.ReclamoState.ESPERANDO_CONFIRMACION.name,
            "confirmation_id": uuid.uuid4().hex, "datos_reclamo": dict(data)}}}
    db.session.commit()  # Fixture draft before Unit ownership.
    _physical(env)
    return data


def test_real_flow_keeps_prefill_and_requires_explicit_confirmation(municipal_env):
    env = municipal_env
    data = _draft(env)
    with _unit(env) as unit:
        flow = municipal.ReclamoFlowHandler(_context(env), env.context, **_args(env, unit))
        response = flow.get_confirmation_message()
        assert data["direccion"] in response["message_body"]
        assert data["nombre"] in response["message_body"]
        assert flow.flow_context["datos_reclamo"] == data
        assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 0
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("text", response["message_body"]),)), runtime=env.runtime)
    assert WhatsAppOutboundAttempt.query.count() == 1 and env.sockets == []


def test_real_confirmed_claim_reaches_typed_writer_but_hf_enrichment_holds(municipal_env, monkeypatch):
    env = municipal_env
    _draft(env)
    assert env.context.anon_id is None and env.context.user_id == env.owner.id
    observed = []
    original = actions.servicio_tickets.crear_nuevo_ticket
    def writer(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.append((kwargs["unit_of_work"], kwargs["conversation_turn_id"], result["id"],
            TicketDomainEffectReceipt.query.count()))
        return result
    monkeypatch.setattr(actions.servicio_tickets, "crear_nuevo_ticket", writer)
    with pytest.raises(ConversationUnitOfWorkError, match="municipal_uow_hf_enrichment_not_ported"):
        with _unit(env) as unit:
            flow = municipal.ReclamoFlowHandler(_context(env), env.context, **_args(env, unit))
            flow.handle_confirmacion(env.event.text, {"action_id": "reclamo_confirmar_si"})
    assert len(observed) == 1 and observed[0][1] == env.claim.turn_id and observed[0][3] == 1
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.sockets == []


@pytest.mark.parametrize("reject_reply", (False, True))
def test_real_suggestion_and_receipt_share_outer_transaction(municipal_env, reject_reply):
    env = municipal_env
    data = _claim_data(env)
    data["ubicacion"] = data.pop("direccion")
    data["descripcion"] = "Synthetic citizen suggestion"
    from services.meta_whatsapp_runtime import MetaRuntimeError
    from contextlib import nullcontext
    expected = pytest.raises(MetaRuntimeError, match="meta_outbound_media_unsupported") if reject_reply else nullcontext()
    with expected:
        with _unit(env) as unit:
            action = actions.HacerSugerenciaActionHandler(_context(env), **_args(env, unit))
            result = action.execute(data)
            assert result["success"] and env.sockets == []
            assert TicketDomainEffectReceipt.query.one().idempotency_key == f"whatsapp:{env.tenant.id}:{env.claim.turn_id}:municipio_suggestion_create"
            reply = MetaReplyBundle((MetaReplyContent("image", "unsupported"),)) if reject_reply else MetaReplyBundle((MetaReplyContent("text", result["message_to_user"]),))
            unit.commit_reply(reply, runtime=env.runtime)
    if reject_reply:
        assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
        assert env.sockets == []
    else:
        assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 1
        assert len(env.sockets) == 1 and WhatsAppOutboundAttempt.query.count() == 1
    assert env.calls["post"] == [] and env.calls["twilio"] == 0


def test_real_responder_confirms_existing_suggestion_without_reasking(municipal_env):
    env = municipal_env
    data = _claim_data(env)
    data["ubicacion"] = data.pop("direccion")
    data["descripcion"] = "Synthetic citizen suggestion"
    env.context.context_data = {CONTEXTO_MUNICIPIO: {
        "estado_conversacion": municipal.ConversationState.ESPERANDO_CONFIRMACION_SUGERENCIA.name,
        "datos_sugerencia": dict(data)}}
    db.session.commit()  # Server-owned draft exists before this claim is processed.
    _physical(env)
    with _unit(env) as unit:
        response = _responder(env, unit)
        assert response["success"] and response["fuente"] == "sugerencia_confirmada"
        ticket = MunicipioTicket.query.one()
        assert ticket.tenant_id == env.tenant.id
        assert ticket.nombre_vecino == data["nombre"] and ticket.detalles == data["descripcion"]
        assert ticket.telefono_vecino == "+" + env.event.contact
        assert ticket.consulta_pin and ticket.consulta_pin in response["message_body"]
        assert TicketDomainEffectReceipt.query.one().idempotency_key == f"whatsapp:{env.tenant.id}:{env.claim.turn_id}:municipio_suggestion_create"
        assert env.sockets == []
        serialized = json.dumps({"draft": env.context.context_data, "response": response}, allow_nan=False)
        assert "unit_of_work" not in serialized and "conversation_turn_id" not in serialized
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("text", response["message_body"]),)), runtime=env.runtime)
    assert MunicipioTicket.query.count() == WhatsAppOutboundAttempt.query.count() == 1
    assert len(env.sockets) == 1 and env.calls["post"] == [] and env.calls["twilio"] == 0


def test_real_handoff_creates_case_comment_and_zero_automatic_messages(municipal_env):
    env = municipal_env
    with _unit(env) as unit:
        action = actions.DerivarHumanoActionHandler(_context(env), **_args(env, unit))
        result = action.execute({"motivo_derivacion": "Synthetic human request"})
        assert result["success"] and env.context.context_data["human_chat_in_progress"] is True
        assert TicketComentario.query.one().municipio_ticket_id == result["data"]["ticket_id"]
        assert TicketDomainEffectReceipt.query.count() == 2 and env.sockets == []
        unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert len(env.sockets) == 2 and MunicipioTicket.query.count() == 1
    assert WhatsAppOutboundAttempt.query.count() == 0


def test_deferred_municipal_socket_failure_cannot_rewind_commit(municipal_env, monkeypatch):
    env = municipal_env
    def fail(_payload):
        raise RuntimeError("synthetic municipal socket failure")
    monkeypatch.setattr(boundary, "_emit_ticket", fail)
    with _unit(env) as unit:
        boundary.municipal_ticket_socket(_context(env), {"tenant_id": env.tenant.id}, **_args(env, unit))
        result = unit.commit_reply(MetaReplyBundle(()), runtime=env.runtime)
    assert result.committed and result.after_commit_failures == ("municipal_ticket_update",)


@pytest.mark.parametrize("wrong", ("other-turn", "other-tenant", "other-recipient", "payload-capability"))
def test_responder_rejects_incoherent_scope_before_config_or_payload_merge(municipal_env, monkeypatch, wrong):
    env = municipal_env
    config = Mock(side_effect=AssertionError("config should not be read"))
    monkeypatch.setattr(municipal, "cargar_configuracion_municipio", config)
    with pytest.raises(ConversationUnitOfWorkError, match="municipal_uow_|conversation_uow_"):
        with _unit(env) as unit:
            if wrong == "other-recipient":
                ctx = _context(env)
                ctx["anon_id"] = "+5491100000999"
                boundary.require_municipal_context(ctx, **_args(env, unit))
            else:
                changes = {"other-turn": {"conversation_turn_id": "different-turn"},
                    "other-tenant": {"tenant_id": env.tenant.id + 1},
                    "payload-capability": {}}[wrong]
                question = {"pregunta": env.event.text, "unit_of_work": {"fake": "client"}} if wrong == "payload-capability" else None
                _responder(env, unit, question, **changes)
    config.assert_not_called()
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 0


@pytest.mark.parametrize("branch", ("survey", "llm", "media", "legacy-confirmation"))
def test_unported_branches_hold_before_real_external_adapter(municipal_env, monkeypatch, branch):
    env = municipal_env
    llm = Mock(side_effect=AssertionError("unported LLM called"))
    monkeypatch.setattr(municipal, "llamar_gemini", llm)
    survey = Mock(side_effect=AssertionError("unported survey called"))
    monkeypatch.setattr(municipal, "_get_encuestas_menu", survey)
    legacy = Mock(side_effect=AssertionError("unported legacy writer called"))
    monkeypatch.setattr(municipal, "_create_legacy_reclamo_from_confirmation", legacy)
    if branch == "legacy-confirmation":
        env.context.context_data = {CONTEXTO_MUNICIPIO: {
            "estado_conversacion": municipal.ConversationState.ESPERANDO_CONFIRMACION_RECLAMO.name,
            "datos_parciales_llm_reclamo": _claim_data(env)}}
        db.session.commit()
        _physical(env)
    with pytest.raises(ConversationUnitOfWorkError, match="municipal_uow_"):
        with _unit(env) as unit:
            if branch == "survey":
                municipal.handle_main_menu_action("mostrar_menu_encuestas", _context(env), env.context, **_args(env, unit))
            elif branch == "llm":
                municipal.handle_llm_interaction(env.app, env.event.text, _context(env), None,
                    env.owner, env.context, env.context.context_data[CONTEXTO_MUNICIPIO], **_args(env, unit))
            elif branch == "media":
                _responder(env, unit, {"pregunta": env.event.text, "es_foto": True, "foto_url": "https://example.invalid/synthetic.jpg"})
            else:
                _responder(env, unit)
    llm.assert_not_called()
    survey.assert_not_called()
    legacy.assert_not_called()
    assert MunicipioTicket.query.count() == WhatsAppOutboundAttempt.query.count() == 0


@pytest.mark.parametrize("path", ("reverse", "address-llm"))
def test_claim_external_address_enrichment_holds_before_adapter(municipal_env, monkeypatch, path):
    env = municipal_env
    geo = Mock(side_effect=AssertionError("geocoder called"))
    parser = Mock(side_effect=AssertionError("address LLM called"))
    monkeypatch.setattr(actions, "obtener_direccion_de_coordenadas", geo)
    monkeypatch.setattr(actions, "parse_direccion", parser)
    data = _claim_data(env)
    data["ubicacion"] = "Calle Sintética 123"  # Explicit, without a known district.
    if path == "reverse":
        data["coordenadas"] = {"lat": -33.0, "lng": -68.0}
    with pytest.raises(ConversationUnitOfWorkError, match="municipal_uow_reverse_geocoding_not_ported|municipal_uow_address_llm_not_ported"):
        with _unit(env) as unit:
            actions.CrearReclamoActionHandler(_context(env), **_args(env, unit)).execute(data)
    geo.assert_not_called()
    parser.assert_not_called()
    assert MunicipioTicket.query.count() == 0


def test_capability_never_becomes_draft_or_model_data(municipal_env):
    env = municipal_env
    _draft(env)
    with _unit(env) as unit:
        context = _context(env)
        flow = municipal.ReclamoFlowHandler(context, env.context, **_args(env, unit))
        response = flow.get_confirmation_message()
        serialized = json.dumps({"draft": env.context.context_data, "response": response}, allow_nan=False)
        assert "unit_of_work" not in serialized and "conversation_turn_id" not in serialized
        assert context.get("unit_of_work") is None and flow.unit_of_work is unit
        unit.commit_reply(MetaReplyBundle((MetaReplyContent("text", response["message_body"]),)), runtime=env.runtime)


@pytest.mark.parametrize("entry", ("confirmed-claim", "unknown-category", "main-menu"))
def test_remote_hf_flags_cannot_reach_client_before_municipal_hold(municipal_env, monkeypatch, entry):
    env = municipal_env
    from services import huggingface_inference_service as hf
    from services import ticket_ai_enrichment as enrichment
    monkeypatch.setenv("HUGGINGFACE_ZERO_SHOT_ENABLED", "true")
    env.app.config["HUGGINGFACE_ALLOW_NETWORK_IN_TESTS"] = True
    # Testing=True alone would mask the unsafe callgraph in V1. Both real
    # conditions must explicitly permit the remote adapter in this control.
    assert hf.zero_shot_enabled() and hf._network_calls_allowed()
    client = Mock(side_effect=AssertionError("HF client must not be reached"))
    builder = Mock(wraps=enrichment.build_ticket_ai_enrichment)
    monkeypatch.setattr(hf, "_get_client", client)
    monkeypatch.setattr(enrichment, "build_ticket_ai_enrichment", builder)
    if entry == "confirmed-claim":
        _draft(env)
    with pytest.raises(ConversationUnitOfWorkError, match="municipal_uow_hf_enrichment_not_ported"):
        with _unit(env) as unit:
            if entry == "confirmed-claim":
                municipal.ReclamoFlowHandler(_context(env), env.context, **_args(env, unit)).handle_confirmacion(
                    env.event.text, {"action_id": "reclamo_confirmar_si"})
            elif entry == "unknown-category":
                municipal.ReclamoFlowHandler(_context(env), env.context, **_args(env, unit)).handle_categoria(
                    "Synthetic undefined municipal issue")
            else:
                municipal.handle_main_menu_action("iniciar_reclamo", _context(env), env.context, **_args(env, unit))
    builder.assert_not_called()
    client.assert_not_called()
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and env.sockets == []
    assert env.calls["post"] == [] and env.calls["twilio"] == 0
