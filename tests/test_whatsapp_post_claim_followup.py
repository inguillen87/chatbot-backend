import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from models import (
    ChatSessionContext,
    MunicipioTicket,
    TenantProfile,
    TicketComentario,
    User,
    WhatsappNumero,
    db,
)
from routes.whatsapp_webhook import (
    WhatsAppDurableReplayError,
    _apply_completed_reclamo_context,
    _durable_replay_failure_or_retry,
    _handle_recent_municipal_ticket_followup,
)
from services.constants import CONTEXTO_MUNICIPIO
from services.response_formatter import build_interactive_response
from services.municipio_responder import ReclamoFlowHandler, ReclamoState
from services.whatsapp_receipts import build_claim_created_followup_text


def _seed_open_followup(*, ticket_state: str = "nuevo"):
    owner = User(
        name="Municipio Follow-up",
        email="followup-municipio@example.test",
        rol="empresa",
        tipo_chat="municipio",
    )
    owner.set_password("followup-test-password")
    db.session.add(owner)
    db.session.flush()
    owner.municipio_id = owner.id

    tenant = TenantProfile(
        slug="followup-municipio",
        nombre="Municipio Follow-up",
        tipo="municipio",
        municipio_id=owner.id,
        is_active=True,
    )
    db.session.add(tenant)
    db.session.flush()
    owner.tenant_id = tenant.id

    ticket = MunicipioTicket(
        pregunta="",
        asunto="Luminaria",
        categoria="Luminaria",
        detalles="Poste caído en el barrio",
        direccion="Don Bosco 56",
        municipio_id=owner.id,
        tenant_id=tenant.id,
        anon_id="+5492613168608",
        nro_ticket="401746",
        consulta_pin="167779",
        estado=ticket_state,
    )
    db.session.add(ticket)
    db.session.flush()

    now = time.time()
    session = ChatSessionContext(
        chat_session_id=f"whatsapp_{owner.id}_+5492613168608",
        user_id=owner.id,
        tenant_id=tenant.id,
        anon_id="+5492613168608",
        context_data={
            "last_options_sent": [
                {"texto": "Confirmar", "action_id": "reclamo_confirmar_si"},
                {"texto": "Cancelar", "action_id": "reclamo_cancelar"},
            ],
            "active_ticket_followup": {
                "ticket_id": ticket.id,
                "ticket_nro": "M-401746",
                "consulta_pin": "167779",
                "tracking_url": (
                    "https://chatboc.test/tracking/claim/401746#pin=167779"
                ),
                "started_at": now,
                "until": now + 3600,
            },
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": "CONVERSACION_GENERAL_LLM",
                "last_created_reclamo": {
                    "ticket_id": ticket.id,
                    "ticket_nro": "M-401746",
                    "consulta_pin": "167779",
                },
            },
        },
    )
    db.session.add(session)
    db.session.commit()
    return owner, tenant, ticket, session


def _handle(session, owner, tenant, text, send):
    return _handle_recent_municipal_ticket_followup(
        session_context=session,
        client_user=owner,
        tenant_profile=tenant,
        end_user=None,
        from_number_cleaned="+5492613168608",
        message_body=text,
        action=None,
        twilio_message_client=send,
        to_number_raw="whatsapp:+5491100000000",
        from_number_raw="whatsapp:+5492613168608",
        now_ts=time.time(),
    )


def test_claim_completion_clears_stale_confirmation_options_and_opens_followup():
    stale = {
        "last_options_sent": [
            {"texto": "Confirmar", "action_id": "reclamo_confirmar_si"}
        ],
        "pending_sensitive_action": {"action_id": "reclamo_confirmar_si"},
        CONTEXTO_MUNICIPIO: {
            "reclamo_flow_v2": {"state": "ESPERANDO_CONFIRMACION"}
        },
    }

    merged = _apply_completed_reclamo_context(
        stale,
        {
            "confirmation_id": "confirmation-401746",
            "ticket_id": 401746,
            "ticket_nro": "M-401746",
            "consulta_pin": "167779",
            "tracking_url": "https://chatboc.test/tracking/claim/401746#pin=167779",
        },
    )

    assert "last_options_sent" not in merged
    assert "pending_sensitive_action" not in merged
    assert "reclamo_flow_v2" not in merged[CONTEXTO_MUNICIPIO]
    assert merged["active_ticket_followup"]["ticket_nro"] == "M-401746"
    assert merged["active_ticket_followup"]["until"] > time.time()


def test_terminal_receipt_formatter_does_not_append_or_store_menu_options():
    formatted = build_interactive_response(
        options=[],
        body_text="Reclamo registrado.",
        channel="whatsapp",
        message_type="text",
        original_bot_response={
            "message_body": "Reclamo registrado.",
            "_suppress_whatsapp_navigation": True,
        },
    )

    assert formatted["text"]["body"] == "Reclamo registrado."
    assert not (formatted.get("contexto_actualizado") or {}).get("last_options_sent")
    assert "Menú" not in formatted["text"]["body"]
    assert "Cancelar" not in formatted["text"]["body"]


def test_callback_receipt_is_explicitly_pending_and_never_claims_call_completed():
    text = build_claim_created_followup_text(
        "167779",
        callback_requested=True,
    )

    assert "solicitud de llamada" in text.lower()
    assert "pendiente" in text.lower()
    assert "no confirma" in text.lower()
    assert "realizado" in text.lower()


def test_guided_claim_adapter_preserves_terminal_navigation_and_audio_flags():
    context_data = {
        CONTEXTO_MUNICIPIO: {
            "reclamo_flow_v2": {
                "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
                "confirmation_id": "confirmation-terminal-flags",
                "datos_reclamo": {
                    "categoria": "Luminaria",
                    "descripcion": "Poste caído",
                    "direccion": "Don Bosco 56",
                    "nombre": "Marcelo",
                    "dni": "32877851",
                    "email": "marcelo@example.test",
                    "telefono": "+5492613168608",
                },
            }
        }
    }
    handler = ReclamoFlowHandler(
        {
            "chat_db_context_data": context_data,
            "chat_session_uuid": "wa-terminal-flags",
            "channel": "whatsapp",
        },
        chat_db_context=None,
    )
    action_result = {
        "success": True,
        "message_body": "Reclamo recibido.",
        "message_type": "text",
        "options_list": [],
        "data": {
            "ticket_id": 401746,
            "nro_ticket": "M-401746",
            "consulta_pin": "167779",
        },
        "_suppress_whatsapp_navigation": True,
        "generar_audio": False,
        "skip_audio_generation": True,
        "_context_keys_to_delete": ["last_options_sent"],
    }

    with patch(
        "services.municipio_responder.CrearReclamoActionHandler"
    ) as action_handler:
        action_handler.return_value.execute.return_value = action_result
        response = handler.handle_confirmacion("confirmar", {})

    assert response["_suppress_whatsapp_navigation"] is True
    assert response["generar_audio"] is False
    assert response["skip_audio_generation"] is True
    assert response["_context_keys_to_delete"] == ["last_options_sent"]


def test_post_claim_pin_question_returns_current_ticket_without_comment_or_reconfirm(client):
    owner, tenant, _ticket, session = _seed_open_followup()
    sender = MagicMock()

    with patch("routes.whatsapp_webhook._send_twilio_message") as send:
        result = _handle(
            session,
            owner,
            tenant,
            "¿Y mi PIN para poder preguntar en el futuro?",
            sender,
        )

    assert result == ("OK", 200)
    assert TicketComentario.query.count() == 0
    body = send.call_args.kwargs["body"]
    assert "M-401746" in body
    assert "167779" in body
    assert "Seguimiento" in body
    assert "confirm" not in body.lower()
    db.session.refresh(session)
    assert "last_options_sent" not in session.context_data


def test_post_claim_comment_is_tenant_scoped_and_added_to_same_ticket(client):
    owner, tenant, ticket, session = _seed_open_followup()

    with patch("routes.whatsapp_webhook._send_twilio_message"):
        result = _handle(
            session,
            owner,
            tenant,
            "La rama también está bloqueando la vereda.",
            MagicMock(),
        )

    assert result == ("OK", 200)
    comment = TicketComentario.query.one()
    assert comment.municipio_ticket_id == ticket.id
    assert comment.comentario == "La rama también está bloqueando la vereda."
    assert comment.origen == "whatsapp"


def test_post_claim_comment_replay_is_idempotent_for_same_durable_turn(client):
    owner, tenant, ticket, session = _seed_open_followup()
    durable_claim = SimpleNamespace(turn_id="turn-followup-001")

    def handle_once():
        return _handle_recent_municipal_ticket_followup(
            session_context=session,
            client_user=owner,
            tenant_profile=tenant,
            end_user=None,
            from_number_cleaned="+5492613168608",
            message_body="La rama también está bloqueando la vereda.",
            action=None,
            twilio_message_client=None,
            to_number_raw="whatsapp:+5491100000000",
            from_number_raw="whatsapp:+5492613168608",
            durable_claim=durable_claim,
            now_ts=time.time(),
        )

    assert handle_once() == ("OK", 200)
    assert handle_once() == ("OK", 200)
    comments = TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).all()
    assert len(comments) == 1
    assert comments[0].comentario == "La rama también está bloqueando la vereda."


def test_post_claim_emoji_is_acknowledged_without_new_comment_or_flow(client):
    owner, tenant, _ticket, session = _seed_open_followup()

    with patch("routes.whatsapp_webhook._send_twilio_message") as send:
        result = _handle(session, owner, tenant, "👍", MagicMock())

    assert result == ("OK", 200)
    assert TicketComentario.query.count() == 0
    assert "sigue abierto" in send.call_args.kwargs["body"]
    db.session.refresh(session)
    municipality = session.context_data[CONTEXTO_MUNICIPIO]
    assert "reclamo_flow_v2" not in municipality


def test_clear_new_claim_request_exits_followup_and_reaches_normal_orchestrator(client):
    owner, tenant, _ticket, session = _seed_open_followup()

    with patch("routes.whatsapp_webhook._send_twilio_message") as send:
        result = _handle(
            session,
            owner,
            tenant,
            "Quiero hacer otro reclamo por un bache.",
            MagicMock(),
        )

    assert result is None
    send.assert_not_called()
    assert TicketComentario.query.count() == 0
    db.session.refresh(session)
    assert "active_ticket_followup" not in session.context_data


def test_durable_failure_raises_for_worker_but_direct_mode_returns_retry():
    source_error = RuntimeError("storage failed")

    assert _durable_replay_failure_or_retry(
        False,
        "response_format_or_session_commit_failed",
        source_error,
    ) == ("RETRY", 503)
    with pytest.raises(WhatsAppDurableReplayError) as raised:
        _durable_replay_failure_or_retry(
            True,
            "response_format_or_session_commit_failed",
            source_error,
        )
    assert str(raised.value) == "response_format_or_session_commit_failed"
    assert raised.value.__cause__ is source_error


def test_webhook_pin_question_short_circuits_bot_after_created_ticket(client, monkeypatch):
    monkeypatch.setitem(
        client.application.config,
        "TWILIO_ALLOW_NETWORK_IN_TESTS",
        True,
    )
    owner, _tenant, _ticket, _session = _seed_open_followup()
    mapping = WhatsappNumero(
        numero_whatsapp="+5491100000000",
        user_id=owner.id,
        is_active=True,
    )
    db.session.add(mapping)
    db.session.commit()

    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-followup-pin")
    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = client.post(
            "/webhook/whatsapp",
            data={
                "To": "whatsapp:+5491100000000",
                "From": "whatsapp:+5492613168608",
                "Body": "Y mi pin para poder preguntar en el futuro?",
                "MessageSid": "SM-followup-pin-inbound",
            },
            headers={"X-Twilio-Signature": "valid"},
        )

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"
    responder.assert_not_called()
    assert MunicipioTicket.query.count() == 1
    sent_bodies = [str(call.kwargs.get("body") or "") for call in twilio.messages.create.call_args_list]
    assert any("167779" in body and "M-401746" in body for body in sent_bodies)
    assert all("No cancele" not in body for body in sent_bodies)


def _post_followup_media(client, owner):
    mapping = WhatsappNumero(
        numero_whatsapp="+5491100000000",
        user_id=owner.id,
        is_active=True,
    )
    db.session.add(mapping)
    db.session.commit()
    download = MagicMock(content=b"image-bytes")
    download.raise_for_status.return_value = None
    return client.post(
        "/webhook/whatsapp",
        data={
            "To": "whatsapp:+5491100000000",
            "From": "whatsapp:+5492613168608",
            "Body": "",
            "MessageSid": "SM-followup-media-inbound",
            "NumMedia": "1",
            "MediaUrl0": "https://media.example.test/evidence.jpg",
            "MediaContentType0": "image/jpeg",
        },
        headers={"X-Twilio-Signature": "valid"},
    )


def test_closed_ticket_media_is_not_attached_or_reinterpreted(client, monkeypatch):
    monkeypatch.setitem(
        client.application.config,
        "TWILIO_ALLOW_NETWORK_IN_TESTS",
        True,
    )
    owner, _tenant, _ticket, session = _seed_open_followup(ticket_state="cerrado")
    attachment = MagicMock(
        id=77,
        url="https://storage.example.test/evidence.jpg",
        mime="image/jpeg",
        nombre_original="evidence.jpg",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-closed-media")
    download = MagicMock(content=b"image-bytes")
    download.raise_for_status.return_value = None

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.requests.get", return_value=download),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch("routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket") as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = _post_followup_media(client, owner)

    assert response.status_code == 200
    attach.assert_not_called()
    intake.assert_not_called()
    responder.assert_not_called()
    db.session.refresh(session)
    assert "active_ticket_followup" not in session.context_data
    sent_bodies = [str(call.kwargs.get("body") or "") for call in twilio.messages.create.call_args_list]
    assert any("ya está cerrado" in body and "No adjunté" in body for body in sent_bodies)


def test_open_ticket_media_is_attached_and_followup_window_stays_active(client, monkeypatch):
    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, _tenant, ticket, session = _seed_open_followup()
    attachment = MagicMock(
        id=79,
        url="https://storage.example.test/evidence.jpg",
        mime="image/jpeg",
        nombre_original="evidence.jpg",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-open-media")
    download = MagicMock(content=b"image-bytes")
    download.raise_for_status.return_value = None

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.requests.get", return_value=download),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch(
            "routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket"
        ) as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = _post_followup_media(client, owner)

    assert response.status_code == 200
    attach.assert_called_once()
    assert attach.call_args.kwargs["ticket"].id == ticket.id
    assert "imagen como evidencia" in attach.call_args.kwargs["comentario_text"]
    intake.assert_not_called()
    responder.assert_not_called()
    db.session.refresh(session)
    assert session.context_data["active_ticket_followup"]["ticket_nro"] == "M-401746"
    assert "awaiting_ticket_photo" not in session.context_data


@pytest.mark.parametrize(
    ("mime_type", "suffix", "expected_guidance"),
    [
        ("image/webp", "webp", "sticker"),
        ("text/vcard", "vcf", "tarjeta de contacto"),
    ],
)
def test_open_ticket_non_evidence_media_is_not_auto_attached(
    client,
    monkeypatch,
    mime_type,
    suffix,
    expected_guidance,
):
    """An open follow-up window is not blanket consent to bind every media."""

    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, _tenant, ticket, session = _seed_open_followup()
    mapping = WhatsappNumero(
        numero_whatsapp="+5491100000000",
        user_id=owner.id,
        is_active=True,
    )
    db.session.add(mapping)
    db.session.commit()
    attachment = MagicMock(
        id=180,
        url=f"https://storage.example.test/followup.{suffix}",
        mime=mime_type,
        nombre_original=f"private-followup.{suffix}",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-non-evidence")

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch(
            "routes.whatsapp_webhook._download_twilio_media",
            return_value=b"bounded-private-media",
        ),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ) as create_attachment,
        patch(
            "routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket"
        ) as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        payload = {
            "To": "whatsapp:+5491100000000",
            "From": "whatsapp:+5492613168608",
            "Body": "private-person.vcf" if suffix == "vcf" else "",
            "MessageSid": f"SM-followup-{suffix}-inbound",
            "NumMedia": "1",
            "MediaUrl0": f"https://media.example.test/followup.{suffix}?token=secret",
            "MediaContentType0": mime_type,
        }
        headers = {"X-Twilio-Signature": "valid"}
        response = client.post(
            "/webhook/whatsapp", data=payload, headers=headers
        )
        duplicate = client.post(
            "/webhook/whatsapp", data=payload, headers=headers
        )

    assert response.status_code == 200
    assert duplicate.status_code == 200
    create_attachment.assert_called_once()
    attach.assert_not_called()
    intake.assert_not_called()
    responder.assert_not_called()
    assert TicketComentario.query.filter_by(municipio_ticket_id=ticket.id).count() == 0
    sent_body = str(twilio.messages.create.call_args.kwargs.get("body") or "")
    assert expected_guidance in sent_body.lower()
    assert "private-person" not in sent_body
    assert "token=secret" not in sent_body
    twilio.messages.create.assert_called_once()
    db.session.refresh(session)
    assert session.context_data["active_ticket_followup"]["ticket_nro"] == "M-401746"


def test_open_ticket_document_is_validated_contextual_evidence(client, monkeypatch):
    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, _tenant, ticket, _session = _seed_open_followup()
    mapping = WhatsappNumero(
        numero_whatsapp="+5491100000000",
        user_id=owner.id,
        is_active=True,
    )
    db.session.add(mapping)
    db.session.commit()
    attachment = MagicMock(
        id=181,
        url="https://storage.example.test/evidence.pdf",
        mime="application/pdf",
        nombre_original="evidence.pdf",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-document-evidence")

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch(
            "routes.whatsapp_webhook._download_twilio_media",
            return_value=b"bounded-pdf",
        ),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch(
            "routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket"
        ) as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = client.post(
            "/webhook/whatsapp",
            data={
                "To": "whatsapp:+5491100000000",
                "From": "whatsapp:+5492613168608",
                "Body": "",
                "MessageSid": "SM-followup-document-inbound",
                "NumMedia": "1",
                "MediaUrl0": "https://media.example.test/evidence.pdf",
                "MediaContentType0": "application/pdf",
            },
            headers={"X-Twilio-Signature": "valid"},
        )

    assert response.status_code == 200
    attach.assert_called_once()
    assert attach.call_args.kwargs["ticket"].id == ticket.id
    assert "archivo como evidencia" in attach.call_args.kwargs["comentario_text"]
    intake.assert_not_called()
    responder.assert_not_called()


def test_real_session_tail_keeps_two_intentional_claims_after_correction_photo_and_pin(
    client,
    monkeypatch,
):
    """Replay the failure tail from the supplied Junin production session.

    The citizen intentionally created Luminaria and Arbolado claims.  A later
    address correction, photo and PIN question must enrich/query Arbolado,
    never re-run its stale confirmation and materialize three extra tickets.
    """

    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, tenant, arbolado_ticket, session = _seed_open_followup()
    arbolado_ticket.asunto = "Arbolado"
    arbolado_ticket.categoria = "Arbolado"
    arbolado_ticket.detalles = "Arbolado en Plaza Junin"
    arbolado_ticket.direccion = "Don Bosco 56 esquina Sarmiento, Junin"

    luminaria_ticket = MunicipioTicket(
        pregunta="",
        asunto="Luminaria",
        categoria="Luminaria",
        detalles="Poste caido en Palmira",
        direccion="Don Bosco 56, Palmira",
        municipio_id=owner.id,
        tenant_id=tenant.id,
        anon_id="+5492613168608",
        nro_ticket="603142",
        consulta_pin="112233",
        estado="nuevo",
    )
    db.session.add(luminaria_ticket)
    db.session.flush()

    stale_context = session.context_data
    stale_context["pending_sensitive_action"] = {
        "action_id": "reclamo_confirmar_si",
    }
    stale_context[CONTEXTO_MUNICIPIO]["reclamo_flow_v2"] = {
        "state": ReclamoState.ESPERANDO_CONFIRMACION.name,
        "confirmation_id": "stale-arbolado-confirmation",
        "datos_reclamo": {
            "categoria": "Arbolado",
            "descripcion": "Arbolado en Plaza Junin",
            "direccion": "Don Bosco 56 esquina Sarmiento, Junin",
        },
    }
    session.context_data = _apply_completed_reclamo_context(
        stale_context,
        {
            "confirmation_id": "stale-arbolado-confirmation",
            "ticket_id": arbolado_ticket.id,
            "ticket_nro": "M-401746",
            "consulta_pin": "167779",
            "tracking_url": "https://chatboc.test/tracking/claim/401746#pin=167779",
        },
    )
    db.session.add(session)
    db.session.commit()

    assert MunicipioTicket.query.count() == 2
    assert "reclamo_flow_v2" not in session.context_data[CONTEXTO_MUNICIPIO]
    assert "pending_sensitive_action" not in session.context_data

    with patch("routes.whatsapp_webhook._send_twilio_message"):
        assert _handle(
            session,
            owner,
            tenant,
            "La direccion correcta es Don Bosco 55 esquina Sarmiento, Plaza Junin.",
            MagicMock(),
        ) == ("OK", 200)

    attachment = MagicMock(
        id=91,
        url="https://storage.example.test/session-tail.jpg",
        mime="image/jpeg",
        nombre_original="session-tail.jpg",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-session-tail")
    download = MagicMock(content=b"image-bytes")
    download.raise_for_status.return_value = None
    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.requests.get", return_value=download),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch("routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket") as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        media_response = _post_followup_media(client, owner)

    assert media_response.status_code == 200
    attach.assert_called_once()
    assert attach.call_args.kwargs["ticket"].id == arbolado_ticket.id
    intake.assert_not_called()
    responder.assert_not_called()

    with patch("routes.whatsapp_webhook._send_twilio_message") as send:
        assert _handle(
            session,
            owner,
            tenant,
            "Y mi PIN para poder consultar en el futuro?",
            MagicMock(),
        ) == ("OK", 200)

    assert "167779" in send.call_args.kwargs["body"]
    assert MunicipioTicket.query.count() == 2
    assert {
        ticket.nro_ticket for ticket in MunicipioTicket.query.order_by(MunicipioTicket.id)
    } == {"401746", "603142"}


def test_open_ticket_audio_keeps_transcript_as_ticket_evidence(client, monkeypatch):
    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, _tenant, ticket, session = _seed_open_followup()
    mapping = WhatsappNumero(
        numero_whatsapp="+5491100000000",
        user_id=owner.id,
        is_active=True,
    )
    db.session.add(mapping)
    db.session.commit()
    attachment = MagicMock(
        id=80,
        url="https://storage.example.test/followup.ogg",
        mime="audio/ogg",
        nombre_original="followup.ogg",
        analisis=None,
    )
    twilio = MagicMock()
    twilio.messages.create.return_value = SimpleNamespace(sid="SM-open-audio")
    download = MagicMock(content=b"audio-bytes")
    download.raise_for_status.return_value = None
    transcript = "El poste también está bloqueando la vereda."

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.requests.get", return_value=download),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch(
            "services.audio_transcription_service.transcribe_audio_bytes",
            return_value=transcript,
        ),
        patch(
            "routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket"
        ) as attach,
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = client.post(
            "/webhook/whatsapp",
            data={
                "To": "whatsapp:+5491100000000",
                "From": "whatsapp:+5492613168608",
                "Body": "",
                "MessageSid": "SM-followup-audio-inbound",
                "NumMedia": "1",
                "MediaUrl0": "https://media.example.test/followup.ogg",
                "MediaContentType0": "audio/ogg",
            },
            headers={"X-Twilio-Signature": "valid"},
        )

    assert response.status_code == 200
    attach.assert_called_once()
    assert attach.call_args.kwargs["ticket"].id == ticket.id
    assert attach.call_args.kwargs["comentario_text"] == f"Nota de voz del vecino: {transcript}"
    intake.assert_not_called()
    responder.assert_not_called()
    db.session.refresh(session)
    assert session.context_data["active_ticket_followup"]["ticket_nro"] == "M-401746"


def test_media_bridge_failure_returns_retry_and_never_falls_into_assisted_intake(client, monkeypatch):
    monkeypatch.setenv("TWILIO_ALLOW_NETWORK_IN_TESTS", "1")
    owner, _tenant, _ticket, _session = _seed_open_followup()
    attachment = MagicMock(
        id=78,
        url="https://storage.example.test/evidence.jpg",
        mime="image/jpeg",
        nombre_original="evidence.jpg",
        analisis=None,
    )
    twilio = MagicMock()
    download = MagicMock(content=b"image-bytes")
    download.raise_for_status.return_value = None

    with (
        patch("routes.whatsapp_webhook.validator") as validator,
        patch("routes.whatsapp_webhook.twilio_client", twilio),
        patch("routes.whatsapp_webhook.requests.get", return_value=download),
        patch(
            "routes.whatsapp_webhook.create_attachment_with_thumbnail",
            return_value=attachment,
        ),
        patch(
            "routes.whatsapp_webhook._attach_whatsapp_adjunto_to_ticket",
            side_effect=RuntimeError("attachment persistence failed"),
        ),
        patch("routes.whatsapp_webhook.create_whatsapp_assisted_intake") as intake,
        patch("routes.whatsapp_webhook.responder_chatboc") as responder,
    ):
        validator.validate.return_value = True
        response = _post_followup_media(client, owner)

    assert response.status_code == 503
    assert response.get_data(as_text=True) == "RETRY"
    intake.assert_not_called()
    responder.assert_not_called()
