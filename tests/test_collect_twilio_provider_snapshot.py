import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest

from scripts import collect_twilio_provider_snapshot as collector
from services.provider_cutover_evidence import CutoverEvidenceError


ACCOUNT = "AC0123456789abcdef0123456789abcdef"
SENDER = "XE0123456789abcdef0123456789abcdef"
SERVICE = "MG0123456789abcdef0123456789abcdef"
WEBHOOK = "https://api.chatboc.ar/webhook/whatsapp"
CALLBACK = "https://api.chatboc.ar/twilio/whatsapp/status"
PROJECT = "prj_0123456789abcdef"
DEPLOYMENT = "dpl_0123456789abcdef"
REVISION = "a" * 40
DATABASE = "b" * 64
BINDING = "c" * 64
OBSERVED = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)


class FakeSource:
    account_sid = ACCOUNT

    def __init__(self, *, status="ONLINE", webhook=WEBHOOK, callback=CALLBACK):
        self.status = status
        self.webhook = webhook
        self.callback = callback
        self.calls = []

    def list_senders(self):
        self.calls.append("list_senders")
        return [
            {"sid": SENDER, "sender_id": "whatsapp:+17432643718"},
            {
                "sid": "XEabcdef0123456789abcdef0123456789",
                "sender_id": "whatsapp:+15005550006",
            },
        ]

    def fetch_sender(self, sender_sid):
        self.calls.append(("fetch_sender", sender_sid))
        return {
            "sid": SENDER,
            "sender_id": "whatsapp:+17432643718",
            "status": self.status,
            "webhook": {
                "callback_url": self.webhook,
                "status_callback_url": self.callback,
            },
        }

    def list_services(self):
        self.calls.append("list_services")
        return [
            {
                "sid": SERVICE,
                "inbound_request_url": WEBHOOK,
                "status_callback": CALLBACK,
            }
        ]

    def list_service_senders(self, service_sid):
        self.calls.append(("list_service_senders", service_sid))
        return [{"sid": SENDER}]


def _document(source=None, **overrides):
    values = dict(
        tenant_id=22,
        credential_environment_variable="JUNIN_TWILIO_AUTH_TOKEN",
        signing_key_environment_variable="CUTOVER_PROVIDER_SNAPSHOT_HMAC_SECRET",
        credential_binding_key_environment_variable="CUTOVER_CREDENTIAL_BINDING_HMAC_SECRET",
        credential_binding_hmac_sha256=BINDING,
        expected_webhook_url=WEBHOOK,
        expected_status_callback_url=CALLBACK,
        destination_project_id=PROJECT,
        destination_deployment_id=DEPLOYMENT,
        destination_deployment_revision=REVISION,
        database_identity_sha256=DATABASE,
        challenge_nonce="runtime-nonce-20260829-001",
        cutover_window_evidence_id="cutover-window-20260829-001",
        evidence_id_value="twilio-provider-read-20260829-001",
        observed_at=OBSERVED,
    )
    values.update(overrides)
    return collector.collect_snapshot_document(source or FakeSource(), **values)


def test_get_only_collector_builds_exact_online_sender_document():
    source = FakeSource()
    document = _document(source)

    assert document["contract_version"] == collector.CONTRACT_VERSION
    assert document["read_only"] is True
    assert document["mutations_performed"] is False
    assert document["messages_sent"] is False
    assert document["sender_count"] == 1
    assert document["sender_status"] == "ONLINE"
    assert document["phone_number"] == "+17432643718"
    assert document["sender_sid"] == SENDER
    assert document["messaging_service_sid"] == SERVICE
    assert document["credential_binding_hmac_sha256"] == BINDING
    assert source.calls == [
        "list_senders",
        ("fetch_sender", SENDER),
        "list_services",
        ("list_service_senders", SERVICE),
    ]


def test_provider_snapshot_envelope_is_canonical_and_never_contains_token():
    signing_key = "snapshot-signing-key-0123456789abcdef"
    token = "provider-token-never-rendered"
    envelope = collector.signed_envelope(_document(), signing_key=signing_key)
    encoded = json.dumps(envelope, sort_keys=True)

    canonical = json.dumps(
        envelope["document"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    assert envelope["document_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert envelope["signature_hmac_sha256"] == hmac.new(
        signing_key.encode(), canonical, hashlib.sha256
    ).hexdigest()
    assert token not in encoded
    assert signing_key not in encoded


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (FakeSource(status="online"), "twilio_sender_not_online"),
        (
            FakeSource(webhook="https://wrong.example.test/webhook"),
            "twilio_sender_webhook_mismatch",
        ),
        (
            FakeSource(callback="https://wrong.example.test/status"),
            "twilio_sender_status_callback_mismatch",
        ),
    ],
)
def test_collector_fails_closed_on_noncanonical_provider_state(source, reason):
    with pytest.raises(CutoverEvidenceError, match=reason):
        _document(source)


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({"senders": [], "meta": {"next_page_url": None}})


def test_network_client_exposes_only_allowlisted_get_without_redirects():
    session = FakeSession()
    client = collector.TwilioV2GetOnlyClient(
        account_sid_value=ACCOUNT,
        auth_token="token-value",
        session=session,
    )
    assert client.list_senders() == []
    assert len(session.calls) == 1
    url, kwargs = session.calls[0]
    assert url == collector.SENDERS_URL
    assert kwargs["params"] == {
        "Channel": "whatsapp",
        "PageSize": "1000",
    }
    assert kwargs["allow_redirects"] is False
    assert kwargs["auth"] == (ACCOUNT, "token-value")
    assert not hasattr(client, "post")
    assert not hasattr(client, "send")


def test_pagination_must_be_exhaustive_for_exactly_one_claim():
    with pytest.raises(
        CutoverEvidenceError,
        match="twilio_provider_pagination_not_exhaustive",
    ):
        collector._list_payload(
            {
                "senders": [],
                "meta": {"next_page_url": "https://messaging.twilio.com/next"},
            },
            "senders",
        )

