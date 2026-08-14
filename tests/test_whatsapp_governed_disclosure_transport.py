from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from routes.whatsapp_webhook import (
    MAX_WHATSAPP_FREEFORM_BODY_LENGTH,
    _dispatch_required_whatsapp_disclosure,
    _required_whatsapp_disclosure_chunks,
    _send_delayed_payload,
    _send_twilio_message,
)
from services.whatsapp_survey_conversation import (
    WHATSAPP_REQUIRED_DISCLOSURE_CONTRACT_VERSION,
)


def _payload(body: str) -> dict:
    return {
        "_whatsapp_required_disclosure": {
            "contract_version": WHATSAPP_REQUIRED_DISCLOSURE_CONTRACT_VERSION,
            "kind": "survey_governance_consent",
            "body": body,
            "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        }
    }


def test_required_disclosure_preserves_exact_long_unicode_text_in_order():
    body = "Consentimiento ciudadano:\n\n" + ("ámbito público 🙂 " * 230)

    chunks = _required_whatsapp_disclosure_chunks(_payload(body))

    assert len(chunks) > 1
    assert "".join(chunks) == body
    assert all(
        len(chunk.encode("utf-8")) <= MAX_WHATSAPP_FREEFORM_BODY_LENGTH
        for chunk in chunks
    )


def test_required_disclosure_rejects_tampered_text_before_transport():
    payload = _payload("Texto aprobado")
    payload["_whatsapp_required_disclosure"]["body"] = "Texto alterado"

    with pytest.raises(
        ValueError,
        match="whatsapp_required_disclosure_hash_mismatch",
    ):
        _required_whatsapp_disclosure_chunks(payload)


def test_required_disclosure_dispatches_all_chunks_with_same_audit_pin():
    body = "Política institucional:\n\n" + ("participación voluntaria " * 120)
    payload = _payload(body)
    accepted = MagicMock(sid="SM-disclosure")

    with patch(
        "routes.whatsapp_webhook._send_twilio_message",
        return_value=accepted,
    ) as send:
        sent = _dispatch_required_whatsapp_disclosure(
            MagicMock(),
            to_number="whatsapp:+17432643718",
            from_number="whatsapp:+5492901123456",
            payload=payload,
        )

    chunks = _required_whatsapp_disclosure_chunks(payload)
    assert len(sent) == len(chunks)
    assert [call.kwargs["body"] for call in send.call_args_list] == chunks
    assert all(
        call.kwargs["_chatboc_policy_metadata"]["disclosure_sha256"]
        == payload["_whatsapp_required_disclosure"]["sha256"]
        for call in send.call_args_list
    )


def test_low_level_transport_rejects_oversized_whatsapp_freeform_body():
    client = MagicMock()

    with pytest.raises(ValueError, match="whatsapp_freeform_body_limit_exceeded"):
        _send_twilio_message(
            client,
            from_="whatsapp:+17432643718",
            to="whatsapp:+5492901123456",
            body="á" * (MAX_WHATSAPP_FREEFORM_BODY_LENGTH + 1),
        )

    client.messages.create.assert_not_called()


def test_delayed_transport_queues_complete_disclosure_before_acceptance_action():
    body = "Consentimiento completo:\n\n" + ("información pública 🙂 " * 150)
    payload = {
        **_payload(body),
        "message_body": "Confirmá tu decisión para continuar.",
        "message_type": "interactive_buttons",
        "options_list": [
            {"texto": "Sí, acepto", "action_id": "consent:accept"},
            {"texto": "No acepto", "action_id": "consent:reject"},
        ],
        "skip_audio_generation": True,
        "_base_url": "https://www.chatboc.ar",
    }
    app = Flask(__name__)
    sent = []

    class ImmediateTimer:
        def __init__(self, _delay, callback):
            self.callback = callback
            self.daemon = False

        def start(self):
            self.callback()

    def accept_send(_client, **params):
        sent.append(params)
        return MagicMock(sid=f"SM-{len(sent)}")

    formatted = {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Confirmá tu decisión para continuar."},
            "action": {"buttons": []},
        },
    }
    with patch(
        "routes.whatsapp_webhook.threading.Timer",
        side_effect=lambda delay, callback: ImmediateTimer(delay, callback),
    ), patch(
        "services.response_formatter.build_interactive_response",
        return_value=formatted,
    ), patch(
        "routes.whatsapp_webhook._send_twilio_message",
        side_effect=accept_send,
    ):
        _send_delayed_payload(
            client=MagicMock(),
            to_number="whatsapp:+17432643718",
            from_number="whatsapp:+5492901123456",
            payload=payload,
            delay=0,
            app=app,
        )

    disclosure_chunks = _required_whatsapp_disclosure_chunks(payload)
    assert [entry["body"] for entry in sent[:-1]] == disclosure_chunks
    assert "persistent_action" in sent[-1]
    assert all(
        len(entry.get("body", "").encode("utf-8"))
        <= MAX_WHATSAPP_FREEFORM_BODY_LENGTH
        for entry in sent
    )
