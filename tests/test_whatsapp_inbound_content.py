from __future__ import annotations

import pytest

from services.whatsapp_inbound_content import (
    classify_twilio_whatsapp_payload,
    honest_unprocessable_reply,
    is_audio_media_type,
    normalize_safe_whatsapp_inbound_context,
    twilio_inbound_media_count,
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


def test_safe_context_preserves_emoji_as_language_input_without_copying_body():
    emoji = "👍🏽"
    content = classify_twilio_whatsapp_payload({**BASE, "Body": emoji})

    safe_context = content.to_safe_context()

    assert safe_context == {
        "contract_version": "whatsapp.inbound_content.v1",
        "kind": "emoji",
        "durable_message_kind": "text",
        "is_language_input": True,
        "has_media": False,
        "media_mime_type": None,
        "evidence_policy": "not_applicable",
        "reason": None,
    }
    assert emoji not in repr(safe_context)


@pytest.mark.parametrize(
    ("mime_type", "kind", "evidence_policy", "allowed"),
    [
        ("image/jpeg", "image", "exact_active_context_only", True),
        ("audio/ogg", "audio", "exact_active_context_only", True),
        ("application/pdf", "document", "exact_active_context_only", True),
        ("video/mp4", "video", "exact_active_context_only", True),
        ("image/webp", "sticker", "never_automatic", False),
        ("text/vcard", "contact", "never_automatic", False),
    ],
)
def test_attachment_evidence_policy_is_explicit_and_fail_closed(
    mime_type, kind, evidence_policy, allowed
):
    content = classify_twilio_whatsapp_payload(
        {
            **BASE,
            "NumMedia": "1",
            "MediaUrl0": "https://provider.test/private",
            "MediaContentType0": mime_type,
        }
    )

    assert content.kind == kind
    assert content.evidence_policy == evidence_policy
    assert content.allows_automatic_ticket_evidence is allowed
    assert "provider.test" not in repr(content.to_safe_context())


def test_safe_context_revalidation_rejects_spoofed_kind_policy_and_private_mime():
    spoofed_contact_as_image = {
        "contract_version": "whatsapp.inbound_content.v1",
        "kind": "image",
        "durable_message_kind": "image",
        "is_language_input": False,
        "has_media": True,
        "media_mime_type": "text/vcard",
        "evidence_policy": "exact_active_context_only",
        "reason": None,
    }
    private_mime = {
        **spoofed_contact_as_image,
        "kind": "document",
        "durable_message_kind": "document",
        "media_mime_type": "private.example/secret",
    }

    assert normalize_safe_whatsapp_inbound_context(spoofed_contact_as_image) is None
    assert normalize_safe_whatsapp_inbound_context(private_mime) is None


def test_safe_context_revalidation_rejects_unsupported_media_with_known_mime():
    spoofed_unsupported = {
        "contract_version": "whatsapp.inbound_content.v1",
        "kind": "unsupported_media",
        "durable_message_kind": "unknown",
        "is_language_input": False,
        "has_media": True,
        "media_mime_type": "application/pdf",
        "evidence_policy": "never_automatic",
        "reason": "missing_media_content_type",
    }

    assert normalize_safe_whatsapp_inbound_context(spoofed_unsupported) is None


@pytest.mark.parametrize("non_boolean", ["true", "false", 0, 1, None])
def test_safe_context_revalidation_requires_exact_boolean_has_media(non_boolean):
    spoofed_boolean = {
        "contract_version": "whatsapp.inbound_content.v1",
        "kind": "emoji",
        "durable_message_kind": "text",
        "is_language_input": True,
        "has_media": non_boolean,
        "media_mime_type": None,
        "evidence_policy": "not_applicable",
        "reason": None,
    }

    assert normalize_safe_whatsapp_inbound_context(spoofed_boolean) is None


def test_multiple_media_count_uses_declared_or_discovered_attachments():
    assert twilio_inbound_media_count({"NumMedia": "2", "MediaUrl0": "one"}) == 2
    assert (
        twilio_inbound_media_count(
            {"NumMedia": "invalid", "MediaUrl0": "one", "MediaUrl1": "two"}
        )
        == 2
    )


def test_multiple_media_fallback_discloses_no_partial_processing():
    reply = honest_unprocessable_reply("multiple_media").lower()

    assert "varios archivos" in reply
    assert "no procesé el lote de forma parcial" in reply
    assert "de a uno" in reply


@pytest.mark.parametrize(
    "kind",
    ["audio", "image", "video", "sticker", "contact", "multiple_media", "unsupported"],
)
def test_honest_fallback_never_claims_understanding_or_creation(kind):
    reply = honest_unprocessable_reply(kind)

    lowered = reply.lower()
    assert reply
    assert "entendí" not in lowered
    assert "reclamo creado" not in lowered
    assert "gestión creada" not in lowered
