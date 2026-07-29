from services.whatsapp_receipts import (
    build_claim_created_followup_text,
    build_claim_created_template_pre_message,
    build_claim_replay_text,
    render_ticket_whatsapp,
)


def test_claim_template_or_fallback_is_one_compact_pii_free_receipt():
    receipt = build_claim_created_template_pre_message(
        ticket_nro="M-12345",
        categoria="Luminaria",
        consulta_pin="654321",
        base_chat_url="https://example.com/chat",
        nombre="Ana Ciudadana",
        direccion="Calle Privada 123",
        within_24h_window=True,
    )

    assert receipt["variables"] == {
        "1": "M-12345",
        "2": "chat/12345#pin=654321",
    }
    assert receipt["body"] == receipt["fallback"]["body"]
    assert "*Código:* M-12345" in receipt["body"]
    assert "https://example.com/tracking/claim/12345#pin=654321" in receipt["body"]
    assert "Ana Ciudadana" not in receipt["body"]
    assert "Calle Privada 123" not in receipt["body"]
    assert "Luminaria" not in receipt["body"]
    assert receipt["metadata"] == {
        "within_24h_window": True,
        "operational_event": "municipal_claim_created",
        "contains_citizen_pii": False,
    }
    assert receipt["template_contract"]["receipt_contract"] == {
        "kind": "municipal_claim_receipt",
        "tracking_required": True,
        "pin_supported": True,
        "contains_citizen_pii": False,
        "tts_allowed": False,
        "surface_role": "primary_receipt",
        "pre_message_key": "_twilio_pre_messages",
    }


def test_claim_followup_only_adds_pin_and_evidence_instructions():
    followup = build_claim_created_followup_text("654321")

    assert "PIN de consulta: *654321*" in followup
    assert "foto, un audio o un comentario" in followup
    assert "mismo reclamo" in followup
    assert "M-" not in followup
    assert "http" not in followup
    assert "Menú" not in followup


def test_claim_template_fallback_window_defaults_fail_closed():
    receipt = build_claim_created_template_pre_message(
        ticket_nro="M-12345",
        categoria="Luminaria",
        consulta_pin="654321",
    )

    assert receipt["metadata"]["within_24h_window"] is False


def test_claim_replay_is_a_single_complete_receipt():
    replay = build_claim_replay_text(
        ticket_nro="M-12345",
        consulta_pin="654321",
        tracking_url="https://example.com/tracking/claim/12345#pin=654321",
    )

    assert "no generamos otro ticket" in replay
    assert "*Código:* M-12345" in replay
    assert "*PIN:* 654321" in replay
    assert "*Seguimiento:* https://example.com/tracking/claim/12345#pin=654321" in replay
    assert "mismo reclamo" in replay


def test_generic_ticket_receipt_uses_canonical_public_tracking_route():
    receipt = render_ticket_whatsapp(
        kind="reclamo",
        nombre="Vecino/a",
        ticket_nro="M-12345",
        categoria="Luminaria",
        descripcion="Poste caído",
        consulta_pin="654321",
        base_chat_url="https://example.com/chat",
        include_menu=False,
    )

    assert "https://example.com/tracking/claim/12345#pin=654321" in receipt["body_text"]
    assert "https://example.com/chat/12345" not in receipt["body_text"]
