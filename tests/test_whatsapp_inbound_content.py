from __future__ import annotations

import pytest

from services.whatsapp_inbound_content import (
    classify_twilio_whatsapp_payload,
    honest_unprocessable_reply,
    is_audio_media_type,
)


BASE = {
    "MessageSid": "SMinbound001",
    "SmsMessageSid": "SMinbound001",
    "SmsStatus": "received",
    "From": "whatsapp:+5492615550101",
    "To": "whatsapp:+5492615550202",
    "NumMedia": "0",
}


@pytest.mark.parametrize(
    ("extra", "expected_kind", "expected_durable_kind"),
    [
        ({"Body": "Necesito ayuda"}, "text", "text"),
        ({"Body": "👍"}, "emoji", "text"),
        (
            {"Body": "❤️", "OriginalRepliedMessageSid": "SMoriginal001"},
            "reaction",
            "text",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.jpg", "MediaContentType0": "image/jpeg"},
            "image",
            "image",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.ogg", "MediaContentType0": "audio/ogg"},
            "audio",
            "audio",
        ),
        (
            {
                "NumMedia": "1",
                "MediaUrl0": "https://media.test/a.ogg",
                "MediaContentType0": "Application/Ogg; codecs=opus",
            },
            "audio",
            "audio",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.pdf", "MediaContentType0": "application/pdf"},
            "document",
            "document",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.mp4", "MediaContentType0": "video/mp4"},
            "video",
            "video",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.webp", "MediaContentType0": "image/webp"},
            "sticker",
            "image",
        ),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/a.vcf", "MediaContentType0": "text/vcard"},
            "contact",
            "contact",
        ),
        ({"Latitude": "-34.6", "Longitude": "-58.4"}, "location", "location"),
        ({"ButtonPayload": "confirmar", "ButtonText": "Confirmar"}, "interactive", "interactive"),
        ({"ListId": "tramites", "ListTitle": "Trámites"}, "interactive", "interactive"),
        (
            {"NumMedia": "1", "MediaUrl0": "https://media.test/no-type"},
            "unsupported_media",
            "unknown",
        ),
        ({}, "unsupported", "unknown"),
    ],
)
def test_signed_message_matrix(extra, expected_kind, expected_durable_kind):
    content = classify_twilio_whatsapp_payload({**BASE, **extra})

    assert content.kind == expected_kind
    assert content.durable_message_kind == expected_durable_kind
    assert content.is_message is True
    assert content.is_event_only is False


@pytest.mark.parametrize(
    ("payload", "kind"),
    [
        (
            {
                "MessageSid": "SMoutbound001",
                "MessageStatus": "delivered",
                "From": "whatsapp:+5492615550202",
                "To": "whatsapp:+5492615550101",
            },
            "status_event",
        ),
        (
            {
                "CallSid": "CAcontrol001",
                "CallStatus": "ringing",
                "From": "whatsapp:+5492615550101",
                "To": "whatsapp:+5492615550202",
            },
            "call_event",
        ),
        ({"EventType": "provider_control"}, "control_event"),
    ],
)
def test_control_events_are_not_messages(payload, kind):
    content = classify_twilio_whatsapp_payload(payload)

    assert content.kind == kind
    assert content.is_event_only is True
    assert content.is_message is False


def test_inbound_received_status_does_not_turn_blank_message_into_status_callback():
    content = classify_twilio_whatsapp_payload(BASE)

    assert content.kind == "unsupported"
    assert content.is_message is True
    assert content.is_event_only is False


def test_audio_mime_variants_share_one_contract():
    for mime_type in (
        "audio/ogg",
        "audio/ogg; codecs=opus",
        "AUDIO/OPUS",
        "application/ogg",
        "Application/Ogg; codecs=opus",
    ):
        assert is_audio_media_type(mime_type) is True


def test_contact_classifier_never_copies_raw_vcard_or_provider_url():
    payload = {
        **BASE,
        "Body": "persona-privada.vcf",
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.test/private/vcard?token=secret",
        "MediaContentType0": "text/vcard",
    }

    content = classify_twilio_whatsapp_payload(payload)
    rendered = repr(content)

    assert content.kind == "contact"
    assert "persona-privada" not in rendered
    assert "api.twilio.test" not in rendered
    assert "secret" not in rendered


@pytest.mark.parametrize("kind", ["audio", "image", "video", "sticker", "contact", "unsupported"])
def test_honest_fallback_never_claims_understanding_or_creation(kind):
    reply = honest_unprocessable_reply(kind)

    lowered = reply.lower()
    assert reply
    assert "entendí" not in lowered
    assert "reclamo creado" not in lowered
    assert "gestión creada" not in lowered
