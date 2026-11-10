"""Signed Meta input through the existing worker, municipal write and native outbox."""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from models import (
    CategoriaTicket, ChatSessionContext, DomainEffectOutbox, MunicipioTicket, TenantProfile, TicketComentario, TicketDomainEffectReceipt,
    WhatsAppInboundTurn, WhatsAppOutboundAttempt, db,
)
from services import whatsapp_inbound_turns as queue, whatsapp_inbound_worker as worker
from services.constants import CONTEXTO_MUNICIPIO
from services.meta_whatsapp_conversation_sink import MetaConversationSink
from services.meta_whatsapp_runtime import APP_SECRETS_KEY, MetaRuntimeError
from services.whatsapp_conversation_uow import ConversationCommitUncertain, ConversationUnitOfWork
from routes.meta_whatsapp_callback import meta_whatsapp_callback_bp
from tests.test_meta_whatsapp_outbox import APP, CONTACT, PHONE, WABA, environment


def _post(environment, *, kind="text", body="", message_id=None, contact=CONTACT,
          point=(0, -68.3), timestamp=None):
    env = environment
    message_id = message_id or env.event.message_id
    message = {
        "id": message_id, "from": contact, "timestamp": str(env.now - 30 if timestamp is None else timestamp), "type": kind,
        kind: ({"body": body} if kind == "text" else {"latitude": point[0], "longitude": point[1]}),
    }
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": PHONE}, "messages": [message]}
    doc = {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [
        {"field": "messages", "value": value}]}]}
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode()
    secret = env.app.config[APP_SECRETS_KEY][APP].encode()
    signature = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
    response = env.app.test_client().post(
        f"/webhook/meta/whatsapp/{APP}", data=raw, content_type="application/json",
        headers={"X-Hub-Signature-256": signature},
    )
    assert response.status_code == 200, response.get_json()
    row = WhatsAppInboundTurn.query.filter_by(provider="meta", provider_message_sid=message_id).one()
    turn_id = row.turn_id
    db.session.commit()
    return turn_id


def _stage(environment, *, followup="valid", kind="text", body="Se cayó otra rama en la plaza"):
    env = environment
    tenant_id, owner_id = env.tenant.id, env.owner.id
    env.app.config.update(
        META_WHATSAPP_CALLBACK_ENABLED=True,
        META_WHATSAPP_CALLBACK_VERIFY_TOKENS={APP: "synthetic-callback-token"},
        META_WHATSAPP_CALLBACK_CANARY_RECIPIENTS_JSON=json.dumps([
            {"app_id": APP, "waba_id": WABA, "phone_number_id": PHONE, "tenant_id": tenant_id, "contact": contact}
            for contact in (CONTACT, "5491198765432")
        ]),
        META_WHATSAPP_INBOUND_WORKER_ENABLED=True,
        DOMAIN_EFFECT_OUTBOX_MODE="queue",
        DOMAIN_EFFECT_OUTBOX_SECRET="synthetic-domain-outbox-" + "x" * 32,
        DOMAIN_EFFECT_OUTBOX_TENANT_IDS=str(tenant_id),
    )
    env.app.register_blueprint(meta_whatsapp_callback_bp)
    env.tenant.tipo = env.owner.tipo_chat = "municipio"
    env.tenant.municipio_id, env.tenant.pyme_id = owner_id, None
    db.session.commit()
    turn_id = _post(env, kind=kind, body=body)
    session = db.session()
    if followup != "missing":
        ticket_tenant_id = tenant_id
        if followup == "foreign_tenant":
            foreign = TenantProfile(slug="synthetic-foreign", nombre="Synthetic foreign",
                                    tipo="municipio", municipio_id=owner_id, is_active=True)
            session.add(foreign)
            session.flush()
            ticket_tenant_id = foreign.id
        ticket = MunicipioTicket(
            tenant_id=ticket_tenant_id, municipio_id=owner_id,
            estado="cerrado" if followup == "closed" else "nuevo",
            nro_ticket="SYNTHETIC-321", telefono_vecino=(
                "+5491198765432" if followup == "foreign_phone" else "+" + CONTACT),
        )
        session.add(ticket)
        session.flush()
        context = session.get(ChatSessionContext, _turn(turn_id).chat_session_id)
        context.context_data = {"active_ticket_followup": {
            "ticket_id": ticket.id,
            "ticket_nro": "DIFFERENT" if followup == "foreign_reference" else ticket.nro_ticket,
            "until": env.now - 1 if followup == "expired" else env.now + 60,
        }}
    session.commit()
    return tenant_id, turn_id


def _turn(turn_id):
    db.session.expire_all()
    return WhatsAppInboundTurn.query.filter_by(turn_id=turn_id).one()


def _forbid_twilio(*_args, **_kwargs):
    raise AssertionError("A Meta turn must never enter the Twilio replay")


def _drain_draft(tenant_id, turn_id):
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1, report
    row = _turn(turn_id)
    context = db.session.get(ChatSessionContext, row.chat_session_id)
    draft = context.context_data[CONTEXTO_MUNICIPIO].get("reclamo_flow_v2")
    attempt = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=row.id).one()
    assert attempt.provider == "meta" and attempt.status == "pending"
    return row.chat_session_id, draft, attempt.payload_json["message"]["text"]["body"]


def _set_draft_category(tenant_id):
    db.session.add(CategoriaTicket(tenant_id=tenant_id, nombre="Arbolado", tipo="ticket"))
    db.session.commit()


def test_signed_new_claim_keeps_description_and_collects_declared_address_without_geocoding(environment, monkeypatch):
    from services import address_normalizer, municipio_responder

    def no_remote(*_args, **_kwargs):
        raise AssertionError("Meta text draft must not invoke remote enrichment")

    monkeypatch.setattr(address_normalizer, "normalize_and_geocode", no_remote)
    monkeypatch.setattr(municipio_responder, "extract_reclamo_details_from_text", no_remote)
    first = "Quiero hacer un reclamo por un árbol caído"
    tenant_id, first_turn = _stage(environment, followup="missing", body=first)
    _set_draft_category(tenant_id)
    chat_id, first_draft, first_reply = _drain_draft(tenant_id, first_turn)
    assert first_draft["state"] == "ESPERANDO_DIRECCION"
    assert first_draft["datos_reclamo"]["categoria"] == "Arbolado"
    assert first_draft["datos_reclamo"]["descripcion"] == "árbol caído"
    assert "dirección" in first_reply and "menú" not in first_reply

    second_turn = _post(environment, body="Sarmiento 123, Junín", message_id="wamid.draft/address+=")
    second_chat_id, second_draft, second_reply = _drain_draft(tenant_id, second_turn)
    assert second_chat_id == chat_id and second_draft["confirmation_id"] == first_draft["confirmation_id"]
    assert second_draft["state"] == "ESPERANDO_DATOS_CONTACTO"
    assert second_draft["datos_reclamo"]["descripcion"] == "árbol caído"
    assert second_draft["datos_reclamo"]["direccion"] == "Sarmiento 123, Junín"
    assert "coordenadas" not in second_draft["datos_reclamo"]
    assert "todavía no fue registrado" in second_reply and "DNI" not in second_reply
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 0
    assert environment.calls["post"] == []


def test_signed_new_claim_uses_inline_address_and_preserves_draft_on_confirm(environment):
    body = "Quiero hacer un reclamo por un árbol caído en Sarmiento 123"
    tenant_id, first_turn = _stage(environment, followup="missing", body=body)
    _set_draft_category(tenant_id)
    _, draft, reply = _drain_draft(tenant_id, first_turn)
    assert draft["datos_reclamo"]["direccion"] == "Sarmiento 123"
    assert draft["datos_reclamo"]["descripcion"] == "árbol caído"
    assert draft["state"] == "ESPERANDO_DATOS_CONTACTO"
    assert "¿Dónde" not in reply and "todavía no fue registrado" in reply
    second_turn = _post(environment, body="Sí, confirmo", message_id="wamid.draft/confirm+=")
    _, persisted, next_reply = _drain_draft(tenant_id, second_turn)
    assert persisted == draft and "todavía no fue registrado" in next_reply
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 0


def test_meta_text_draft_keeps_separate_contact_and_rejects_bare_number_as_address(environment):
    tenant_id, first_turn = _stage(
        environment, followup="missing", body="Quiero hacer un reclamo por un árbol caído",
    )
    _set_draft_category(tenant_id)
    chat_id, draft, _ = _drain_draft(tenant_id, first_turn)
    other_turn = _post(environment, body="Sarmiento 123", message_id="wamid.draft/other+=",
                       contact="5491198765432")
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 0 and report["results"][0]["turn_id"] == other_turn
    assert _turn(other_turn).chat_session_id != chat_id
    assert _turn(other_turn).last_error_code == "meta_municipal_followup_missing"
    bare_number = _post(environment, body="123456", message_id="wamid.draft/number+=")
    _, same_draft, reply = _drain_draft(tenant_id, bare_number)
    assert same_draft == draft and "Escribí la dirección" in reply


def test_signed_shared_location_completes_draft_step_without_geocoding_or_address(environment, monkeypatch):
    from services import address_normalizer, herramientas_municipio

    def no_geocoding(*_args, **_kwargs):
        raise AssertionError("Shared coordinates must not reach geocoding")

    monkeypatch.setattr(address_normalizer, "normalize_and_geocode", no_geocoding)
    monkeypatch.setattr(herramientas_municipio, "obtener_direccion_de_coordenadas", no_geocoding)
    tenant_id, first_turn = _stage(
        environment, followup="missing", body="Quiero hacer un reclamo por un árbol caído",
    )
    _set_draft_category(tenant_id)
    chat_id, original, _ = _drain_draft(tenant_id, first_turn)
    assert original["state"] == "ESPERANDO_DIRECCION"
    location_turn = _post(environment, kind="location", message_id="wamid.draft/gps+=",
                          point=(0, -68.30890123456))
    same_chat, saved, reply = _drain_draft(tenant_id, location_turn)
    assert same_chat == chat_id and saved["confirmation_id"] == original["confirmation_id"]
    assert saved["state"] == "ESPERANDO_DATOS_CONTACTO"
    values = saved["datos_reclamo"]
    assert values["coordenadas"] == {"lat": 0, "lon": -68.30890123456}
    assert values["ubicacion_origen"] == "whatsapp_location"
    assert values["categoria"] == "Arbolado" and values["descripcion"] == "árbol caído"
    assert not {"direccion", "direccion_geocodificada"}.intersection(values)
    assert "ubicación compartida" in reply and "¿Dónde" not in reply
    assert "todavía no fue registrado" in reply
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == 0

    text_turn = _post(environment, body="Sí, confirmo", message_id="wamid.draft/gpsconfirm+=")
    _, still_saved, next_reply = _drain_draft(tenant_id, text_turn)
    assert still_saved == saved and "ubicación compartida" in next_reply
    assert MunicipioTicket.query.count() == 0 and environment.calls["post"] == []


def test_location_cannot_edit_another_phone_or_overwrite_completed_location_step(environment):
    tenant_id, first_turn = _stage(
        environment, followup="missing", body="Quiero hacer un reclamo por un árbol caído",
    )
    _set_draft_category(tenant_id)
    chat_id, original, _ = _drain_draft(tenant_id, first_turn)
    other = _post(environment, kind="location", message_id="wamid.draft/gps-other+=",
                  contact="5491198765432")
    other_report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert (other_report["turn_id"], other_report["error_code"]) == (other, "meta_inbound_worker_kind_unsupported")
    assert _turn(other).chat_session_id != chat_id
    assert db.session.get(ChatSessionContext, chat_id).context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"] == original

    declared = _post(environment, body="Sarmiento 123", message_id="wamid.draft/gps-declared+=")
    _, saved, _ = _drain_draft(tenant_id, declared)
    second = _post(environment, kind="location", message_id="wamid.draft/gps-overwrite+=")
    later = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert (later["turn_id"], later["error_code"]) == (second, "meta_inbound_worker_kind_unsupported")
    assert _turn(second).chat_session_id == chat_id
    assert db.session.get(ChatSessionContext, chat_id).context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"] == saved
    assert saved["datos_reclamo"]["direccion"] == "Sarmiento 123"
    assert WhatsAppOutboundAttempt.query.count() == 2 and MunicipioTicket.query.count() == 0


def _native_claim_configuration(environment, *, required=None):
    profile = environment.tenant
    profile.configuracion = {
        "whatsapp_meta_native_claim": {"enabled": True, "domain_effects": "municipal_existing"},
        "campos_requeridos_reclamo": (
            ["categoria", "descripcion", "ubicacion", "telefono"] if required is None else required
        ),
        "whatsapp_receipt_header": {"enabled": False},
    }
    db.session.commit()


def _prepare_confirmable_claim(environment, *, gps=False, required=None):
    body = "Quiero hacer un reclamo por un árbol caído"
    if not gps:
        body += " en Sarmiento 123"
    tenant_id, turn_id = _stage(environment, followup="missing", body=body)
    _set_draft_category(tenant_id)
    _native_claim_configuration(environment, required=required)
    if gps:
        _drain_draft(tenant_id, turn_id)
        turn_id = _post(environment, kind="location", message_id="wamid.confirmable/location+=",
                        point=(0, -68.30890123456))
    chat_id, draft, reply = _drain_draft(tenant_id, turn_id)
    return tenant_id, chat_id, draft, reply


def _confirm_code(reply):
    match = re.search(r"CONFIRMAR ([0-9A-F]{8})", reply)
    assert match is not None, reply
    return match.group(1)


def _post_confirmation(environment, code, suffix, *, contact=CONTACT, timestamp=None):
    return _post(environment, body=f"CONFIRMAR {code}", message_id=f"wamid.confirmable/{suffix}+=",
                 contact=contact, timestamp=environment.clock[0] if timestamp is None else timestamp)


@pytest.mark.parametrize("gps", [False, True])
def test_native_confirmation_commits_exactly_one_ticket_and_typed_meta_receipt(environment, gps):
    tenant_id, chat_id, draft, reply = _prepare_confirmable_claim(environment, gps=gps)
    code = _confirm_code(reply)
    assert draft["state"] == "ESPERANDO_CONFIRMACION"
    assert code not in draft["meta_claim_confirmation"].values()
    assert MunicipioTicket.query.count() == 0
    confirm_turn = _post_confirmation(environment, code, "confirm")
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1 and report["results"][0]["outbound_count"] == 2, report
    stored = _turn(confirm_turn)
    ticket = MunicipioTicket.query.one()
    assert ticket.tenant_id == tenant_id and ticket.user_id is None
    assert ticket.anon_id == ticket.telefono_vecino == "+" + CONTACT
    assert ticket.dni_vecino is None and ticket.email_vecino is None
    assert ticket.detalles == "árbol caído" and ticket.categoria == "Arbolado"
    assert ticket.categoria_id == CategoriaTicket.query.filter_by(tenant_id=tenant_id, nombre="Arbolado").one().id
    if gps:
        assert ticket.direccion is None and ticket.latitud == 0 and ticket.longitud == -68.30890123456
        assert ticket.datos_extra["ubicacion_origen"] == "whatsapp_location"
    else:
        assert ticket.direccion == "Sarmiento 123" and ticket.latitud is None and ticket.longitud is None
        assert ticket.datos_extra["ubicacion_origen"] == "direccion_declarada"
    assert ticket.datos_extra["whatsapp_claim_confirmation_id"] == draft["confirmation_id"]
    assert TicketDomainEffectReceipt.query.filter_by(tenant_id=tenant_id, resource_id=ticket.id).count() == 1
    assert DomainEffectOutbox.query.filter_by(tenant_id=tenant_id).count() == 3
    receipts = WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=stored.id).order_by(
        WhatsAppOutboundAttempt.sequence_no,
    ).all()
    assert [row.message_kind for row in receipts] == ["text", "text"]
    bodies = [row.payload_json["message"]["text"]["body"] for row in receipts]
    assert f"M-{ticket.nro_ticket}" in bodies[0] and "Seguimiento" in bodies[0]
    assert "comentario de texto" in bodies[1] and ticket.consulta_pin in bodies[1]
    assert not any(word in "\n".join(bodies) for word in ("foto", "audio", CONTACT, "Sarmiento"))
    context = db.session.get(ChatSessionContext, chat_id).context_data
    assert "reclamo_flow_v2" not in context[CONTEXTO_MUNICIPIO]
    assert context["active_ticket_followup"]["ticket_id"] == ticket.id
    assert environment.calls["post"] == []
    assert _post_confirmation(environment, code, "confirm") == confirm_turn
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    _post_confirmation(environment, code, "repeat-fresh-id")
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["completed"] == 1
    assert MunicipioTicket.query.count() == 1 and TicketComentario.query.count() == 0
    assert TicketDomainEffectReceipt.query.count() == 1 and environment.calls["post"] == []


def test_native_confirmation_rejects_stale_and_foreign_recipient_before_matching_code(environment):
    tenant_id, _, draft, reply = _prepare_confirmable_claim(environment)
    code = _confirm_code(reply)
    for suffix, recipient, timestamp, sent_code in (
        ("foreign", "5491198765432", environment.now, code),
        ("earlier", CONTACT, environment.now - 10, code),
        ("wrong", CONTACT, environment.now, ("0" if code[0] != "0" else "1") + code[1:]),
    ):
        turn_id = _post_confirmation(environment, sent_code, suffix, contact=recipient, timestamp=timestamp)
        result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
        assert result["completed"] == 1 and _turn(turn_id).status == "completed"
        assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0
    tenant_id = environment.tenant.id
    db.session.add(CategoriaTicket(tenant_id=tenant_id, nombre="Luminaria", tipo="ticket"))
    db.session.commit()
    changed = _post_confirmation(environment, code, "catalog-change")
    _, next_draft, next_reply = _drain_draft(tenant_id, changed)
    assert next_draft["confirmation_id"] == draft["confirmation_id"]
    assert next_draft["state"] == "ESPERANDO_CATEGORIA" and "meta_claim_confirmation" not in next_draft
    assert "categoría" in next_reply and MunicipioTicket.query.count() == 0


def test_expired_confirmation_requires_a_new_code_and_never_reuses_previous_proof(environment, monkeypatch):
    tenant_id, _, draft, reply = _prepare_confirmable_claim(environment)
    code = _confirm_code(reply)
    environment.clock[0] = environment.now + 600
    monkeypatch.setattr(queue, "_utc_now", lambda: datetime.fromtimestamp(environment.clock[0], timezone.utc))
    expired = _post_confirmation(environment, code, "expired")
    _, next_draft, next_reply = _drain_draft(tenant_id, expired)
    assert next_draft["confirmation_id"] == draft["confirmation_id"]
    assert next_draft["meta_claim_confirmation"]["issued_at"] == environment.clock[0]
    assert _confirm_code(next_reply) != code and MunicipioTicket.query.count() == 0


def test_native_confirmation_rolls_back_ticket_domain_effects_and_nonce_when_receipt_fails(environment, monkeypatch):
    tenant_id, chat_id, draft, reply = _prepare_confirmable_claim(environment)
    code = _confirm_code(reply)
    confirm = _post_confirmation(environment, code, "receipt-rejected")
    with monkeypatch.context() as patch:
        def reject_receipt(self, **_kwargs):
            assert MunicipioTicket.query.count() == 1 and DomainEffectOutbox.query.count() == 3
            self.unit.fail("synthetic_meta_receipt_rejected")
        patch.setattr(MetaConversationSink, "emit_claim_receipt", reject_receipt)
        result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert (result["status"], result["error_code"]) == ("dead", "synthetic_meta_receipt_rejected")
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=_turn(confirm).id).count() == 0
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0
    assert db.session.get(ChatSessionContext, chat_id).context_data[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"] == draft
    _post_confirmation(environment, code, "retry-new-message")
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["completed"] == 1
    assert MunicipioTicket.query.count() == 1 and environment.calls["post"] == []


def test_native_policy_with_extra_personal_field_holds_without_asking_for_it(environment):
    _, _, draft, reply = _prepare_confirmable_claim(environment, required=["descripcion", "ubicacion", "dni"])
    assert draft["state"] == "ESPERANDO_DATOS_CONTACTO" and "meta_claim_confirmation" not in draft
    assert "todavía no fue registrado" in reply and "DNI" not in reply
    assert MunicipioTicket.query.count() == DomainEffectOutbox.query.count() == 0


def test_native_ticket_writer_rejects_foreign_catalog_id_before_creating_anything(environment, monkeypatch):
    from services.ticket_service import servicio_tickets

    tenant_id, _, _, reply = _prepare_confirmable_claim(environment)
    foreign = TenantProfile(slug="other-category", nombre="Other catalog",
                            tipo="municipio", municipio_id=environment.owner.id, is_active=True)
    db.session.add(foreign)
    db.session.flush()
    category = CategoriaTicket(tenant_id=foreign.id, nombre="Arbolado", tipo="ticket")
    db.session.add(category)
    db.session.flush()
    foreign_category_id = category.id
    db.session.commit()
    original = servicio_tickets.crear_nuevo_ticket

    def with_foreign_catalog_id(*args, **kwargs):
        kwargs["municipal_category_id"] = foreign_category_id
        return original(*args, **kwargs)

    monkeypatch.setattr(servicio_tickets, "crear_nuevo_ticket", with_foreign_catalog_id)
    turn_id = _post_confirmation(environment, _confirm_code(reply), "foreign-category")
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert (result["status"], result["error_code"]) == ("dead", "municipal_ticket_category_scope_invalid")
    assert WhatsAppOutboundAttempt.query.filter_by(inbound_turn_id=_turn(turn_id).id).count() == 0
    assert MunicipioTicket.query.count() == TicketDomainEffectReceipt.query.count() == DomainEffectOutbox.query.count() == 0


def test_signed_meta_comment_runs_once_through_worker_and_native_outbox(environment, monkeypatch):
    tenant_id, turn_id = _stage(environment)
    monkeypatch.setattr(worker, "_load_claim_scope", _forbid_twilio)
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)
    assert report["completed"] == 1 and report["results"][0]["outbound_count"] == 1, report
    row = _turn(turn_id)
    assert row.status == "completed" and row.last_error_code is None
    assert row.result_json["delivery_verified"] is False and "input" not in row.payload_json
    comment = TicketComentario.query.one()
    assert comment.comentario == "Se cayó otra rama en la plaza"
    assert comment.municipio_ticket_id == MunicipioTicket.query.one().id
    assert TicketDomainEffectReceipt.query.one().tenant_id == tenant_id
    attempt = WhatsAppOutboundAttempt.query.one()
    assert attempt.provider == "meta" and attempt.status == "pending"
    assert environment.calls["post"] == []
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "accepted"
    assert worker.dispatch_next_whatsapp_outbound_attempt(tenant_id=tenant_id).status == "idle"
    assert len(environment.calls["post"]) == 1 and environment.calls["twilio"] == 0
    assert environment.calls["post"][0]["json"]["to"] == CONTACT
    assert TicketComentario.query.count() == 1


@pytest.mark.parametrize(("followup", "kind", "body", "code"), [
    ("missing", "text", "Otra rama", "meta_municipal_followup_missing"),
    ("expired", "text", "Otra rama", "meta_municipal_followup_expired_or_invalid"),
    ("foreign_phone", "text", "Otra rama", "meta_municipal_followup_ticket_scope_invalid"),
    ("foreign_reference", "text", "Otra rama", "meta_municipal_followup_ticket_scope_invalid"),
    ("foreign_tenant", "text", "Otra rama", "meta_municipal_followup_ticket_scope_invalid"),
    ("closed", "text", "Otra rama", "meta_municipal_followup_ticket_scope_invalid"),
    ("valid", "location", "", "meta_inbound_worker_kind_unsupported"),
    ("valid", "text", "Quiero adjuntar una foto", "meta_municipal_followup_intent_unsupported"),
])
def test_unsupported_or_unbound_meta_never_writes_or_sends(
    environment, monkeypatch, followup, kind, body, code,
):
    tenant_id, turn_id = _stage(environment, followup=followup, kind=kind, body=body)
    monkeypatch.setattr(worker, "_load_claim_scope", _forbid_twilio)
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert (result["status"], result["error_code"]) == ("dead", code)
    assert _turn(turn_id).last_error_code == code
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and environment.calls["post"] == []


def test_followup_expiring_while_loading_ticket_is_not_written(environment):
    tenant_id, turn_id = _stage(environment)

    def expire_followup(_connection, _cursor, statement, *_args):
        if statement.lstrip().lower().startswith("select") and "from municipio_ticket" in statement.lower():
            environment.clock[0] = environment.now + 60

    event.listen(db.engine, "after_cursor_execute", expire_followup)
    try:
        result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    finally:
        event.remove(db.engine, "after_cursor_execute", expire_followup)
    assert (result["status"], result["error_code"]) == ("dead", "meta_municipal_followup_expired_or_invalid")
    assert _turn(turn_id).last_error_code == "meta_municipal_followup_expired_or_invalid"
    assert TicketComentario.query.count() == WhatsAppOutboundAttempt.query.count() == 0


def test_transient_finalizer_failure_rolls_back_municipal_comment_and_releases_for_retry(environment, monkeypatch):
    tenant_id, turn_id = _stage(environment)

    def offline_transient(*_args, **_kwargs):
        raise MetaRuntimeError("synthetic_authority_temporarily_unavailable", transient=True)

    monkeypatch.setattr(type(environment.runtime), "load_binding", offline_transient)
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert result["status"] == "retry_wait", result
    row = _turn(turn_id)
    assert row.last_error_code == "synthetic_authority_temporarily_unavailable" and row.lease_token is None
    assert TicketComentario.query.count() == TicketDomainEffectReceipt.query.count() == 0
    assert WhatsAppOutboundAttempt.query.count() == 0 and environment.calls["post"] == []


def test_commit_acknowledgement_loss_reports_uncertainty_without_retry(environment, monkeypatch):
    tenant_id, turn_id = _stage(environment)
    complete = ConversationUnitOfWork.commit_reply

    def lose_acknowledgement(unit, *args, **kwargs):
        complete(unit, *args, **kwargs)
        raise ConversationCommitUncertain("synthetic_commit_acknowledgement_lost")

    monkeypatch.setattr(ConversationUnitOfWork, "commit_reply", lose_acknowledgement)
    result = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["results"][0]
    assert result["status"] == "commit_uncertain", result
    assert _turn(turn_id).status == "completed"
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=1)["processed"] == 0
    assert TicketComentario.query.count() == WhatsAppOutboundAttempt.query.count() == 1


def test_provider_selection_leaves_disabled_meta_and_unknown_untouched(environment, monkeypatch):
    tenant_id, meta_id = _stage(environment)
    now = datetime.fromtimestamp(environment.now, timezone.utc)
    environment.app.config["META_WHATSAPP_INBOUND_WORKER_ENABLED"] = False
    receipts = {}
    for provider in ("twilio", "future"):
        receipts[provider] = queue.ingest_whatsapp_inbound_turn(
            tenant_id=tenant_id, provider=provider, provider_message_sid=f"synthetic-{provider}",
            stream_key=f"separate-{provider}", payload={"Body": "Synthetic"}, now=now,
        )
    monkeypatch.setattr(worker, "_load_claim_scope", lambda _claim: (None, None, SimpleNamespace(account_sid="")))
    from routes import whatsapp_webhook
    monkeypatch.setattr(whatsapp_webhook, "whatsapp_webhook", lambda: ("OK", 200))
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=3)
    assert report["completed"] == 1 and report["results"][0]["turn_id"] == receipts["twilio"].turn_id
    assert _turn(meta_id).status == "received" and _turn(meta_id).attempt_count == 0
    assert _turn(receipts["future"].turn_id).status == "received"
    environment.app.config["META_WHATSAPP_INBOUND_WORKER_ENABLED"] = True
    environment.app.config["DOMAIN_EFFECT_OUTBOX_MODE"] = "legacy"
    assert worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=3)["processed"] == 0
    assert _turn(meta_id).status == "received" and _turn(meta_id).attempt_count == 0
    environment.app.config["DOMAIN_EFFECT_OUTBOX_MODE"] = "queue"
    report = worker.process_whatsapp_inbound_stream(tenant_id=tenant_id, limit=3)
    assert report["completed"] == 1, report
    assert _turn(receipts["future"].turn_id).status == "received"
