from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import pytest

from database import db
from models import WebhookDelivery
import services.omnichannel_service as omni_service
from routes.omnichannel import omnichannel_bp
from services.omnichannel_adapter_security import (
    CONTRACT_VERSION,
    NormalizedAdapterEvent,
    OmnichannelAdapterError,
    VerifiedAdapterRequest,
    build_adapter_signature,
    claim_verified_adapter_delivery,
    derive_connection_signing_secret,
    normalize_adapter_event,
    verify_adapter_request,
)
from services.webhook_delivery_service import (
    CLAIMED,
    DUPLICATE,
    IN_PROGRESS,
    complete_delivery,
    fail_delivery,
)


ROOT_SECRET = "adversarial-omnichannel-root-" + ("x" * 32)
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _connection(*, connection_id=12, tenant_id=7, relation_tenant_id=None):
    tenant = SimpleNamespace(
        id=relation_tenant_id if relation_tenant_id is not None else tenant_id,
        is_active=True,
        municipio_id=70,
        pyme_id=None,
    )
    return SimpleNamespace(
        id=connection_id,
        tenant_id=tenant_id,
        provider="telegram",
        channel="telegram",
        status="active",
        config={},
        tenant=tenant,
    )


def _verified(*, event_id="evt-adversarial-1", payload_digest="d" * 64):
    connection = _connection()
    return VerifiedAdapterRequest(
        connection=connection,
        tenant=connection.tenant,
        provider_namespace="omnichannel:12:telegram",
        event_id=event_id,
        event_type="message.created",
        timestamp=int(NOW.timestamp()),
        payload_digest=payload_digest,
        connection_secret=derive_connection_signing_secret(ROOT_SECRET, connection),
    )


def _payload(*, contact=None, message=None):
    return {
        "contract_version": CONTRACT_VERSION,
        "direction": "inbound",
        "channel": "telegram",
        "event_type": "message.created",
        "contact": contact
        or {
            "external_id": "provider-user-123",
            "name": "Vecina",
            "email": "vecina@example.com",
            "phone": "+5491112345678",
        },
        "message": message or {"kind": "text", "text": "Hola"},
    }


def _adapter_client(*, max_payload_bytes=1024):
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        OMNICHANNEL_SIGNED_INBOUND_MODE="enforce",
        OMNICHANNEL_INBOUND_HMAC_SECRET_V1=ROOT_SECRET,
        OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES=max_payload_bytes,
    )
    app.register_blueprint(omnichannel_bp)
    return app.test_client()


@pytest.fixture()
def adapter_delivery_db(tmp_path):
    database_path = (tmp_path / "adapter-delivery.sqlite3").as_posix()
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{database_path}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={"connect_args": {"check_same_thread": False}},
    )
    db.init_app(app)

    with app.app_context():
        WebhookDelivery.__table__.create(db.engine)
        yield
        db.session.remove()
        WebhookDelivery.__table__.drop(db.engine)
        db.engine.dispose()


def _signature_headers(connection, body, *, event_id="evt-bound-1"):
    timestamp = int(NOW.timestamp())
    event_type = "message.created"
    connection_secret = derive_connection_signing_secret(ROOT_SECRET, connection)
    return {
        "X-Chatboc-Timestamp": str(timestamp),
        "X-Chatboc-Event-Id": event_id,
        "X-Chatboc-Event-Type": event_type,
        "X-Chatboc-Signature-V1": build_adapter_signature(
            connection_secret,
            timestamp=timestamp,
            connection_id=connection.id,
            event_id=event_id,
            event_type=event_type,
            raw_body=body,
        ),
    }


def test_route_rejects_oversize_before_signature_or_database_lookup():
    client = _adapter_client(max_payload_bytes=1024)
    with patch("routes.omnichannel.verify_adapter_request") as verify:
        response = client.post(
            "/omnichannel/adapters/12/inbound",
            data=b"{" + (b"x" * 1024) + b"}",
            content_type="application/json",
        )

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "adapter_payload_too_large"
    verify.assert_not_called()


def test_signature_cannot_be_replayed_across_connections_or_tenants():
    source_connection = _connection(connection_id=12, tenant_id=7)
    target_connection = _connection(connection_id=13, tenant_id=8)
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    config = {
        "OMNICHANNEL_SIGNED_INBOUND_MODE": "enforce",
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1": ROOT_SECRET,
    }
    fake_db = SimpleNamespace(
        session=SimpleNamespace(get=lambda _model, _object_id: target_connection)
    )

    with patch("services.omnichannel_adapter_security.db", fake_db), pytest.raises(
        OmnichannelAdapterError
    ) as caught:
        verify_adapter_request(
            config=config,
            connection_id=target_connection.id,
            headers=_signature_headers(source_connection, body),
            raw_body=body,
            now=NOW,
        )

    assert caught.value.code == "invalid_adapter_signature"


def test_connection_tenant_relation_mismatch_fails_before_signature_use():
    mismatched = _connection(
        connection_id=12,
        tenant_id=7,
        relation_tenant_id=8,
    )
    body = json.dumps(_payload(), separators=(",", ":")).encode("utf-8")
    config = {
        "OMNICHANNEL_SIGNED_INBOUND_MODE": "enforce",
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1": ROOT_SECRET,
    }
    fake_db = SimpleNamespace(
        session=SimpleNamespace(get=lambda _model, _object_id: mismatched)
    )

    with patch("services.omnichannel_adapter_security.db", fake_db), pytest.raises(
        OmnichannelAdapterError
    ) as caught:
        verify_adapter_request(
            config=config,
            connection_id=mismatched.id,
            headers=_signature_headers(mismatched, body),
            raw_body=body,
            now=NOW,
        )

    assert caught.value.code == "adapter_connection_not_found"


def test_adapter_claim_keeps_event_binding_immutable_across_failed_retry(
    adapter_delivery_db,
):
    first_body = b'{"version":1}'
    second_body = b'{"version":2}'
    first_verified = _verified(
        event_id="evt-immutable-retry",
        payload_digest=hashlib.sha256(first_body).hexdigest(),
    )
    conflicting_verified = _verified(
        event_id="evt-immutable-retry",
        payload_digest=hashlib.sha256(second_body).hexdigest(),
    )

    first = claim_verified_adapter_delivery(first_verified, first_body, now=NOW)
    assert first.outcome == CLAIMED
    assert fail_delivery(
        first.delivery_id,
        first.attempts,
        "upstream timeout",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(OmnichannelAdapterError) as caught:
        claim_verified_adapter_delivery(
            conflicting_verified,
            second_body,
            now=NOW + timedelta(seconds=2),
        )

    assert caught.value.code == "adapter_event_payload_conflict"
    receipt = db.session.get(WebhookDelivery, first.delivery_id)
    assert receipt.payload_digest == first_verified.payload_digest
    assert receipt.attempts == 1
    assert receipt.status == WebhookDelivery.STATUS_FAILED

    retry = claim_verified_adapter_delivery(
        first_verified,
        first_body,
        now=NOW + timedelta(seconds=3),
    )
    assert retry.outcome == CLAIMED
    assert retry.attempts == 2


def test_adapter_claim_preserves_processing_stale_and_processed_states(
    adapter_delivery_db,
):
    body = b'{"state":"stable"}'
    verified = _verified(
        event_id="evt-state-machine",
        payload_digest=hashlib.sha256(body).hexdigest(),
    )

    first = claim_verified_adapter_delivery(verified, body, now=NOW)
    active = claim_verified_adapter_delivery(
        verified,
        body,
        now=NOW + timedelta(minutes=1),
    )
    assert active.outcome == IN_PROGRESS
    assert active.attempts == 1

    reclaimed = claim_verified_adapter_delivery(
        verified,
        body,
        now=NOW + timedelta(minutes=6),
    )
    assert reclaimed.outcome == CLAIMED
    assert reclaimed.attempts == 2
    assert complete_delivery(reclaimed.delivery_id, reclaimed.attempts)

    duplicate = claim_verified_adapter_delivery(
        verified,
        body,
        now=NOW + timedelta(minutes=7),
    )
    assert duplicate.outcome == DUPLICATE
    assert duplicate.attempts == 2


def test_scoped_delivery_and_ticket_ids_do_not_persist_raw_provider_event_id():
    raw_event_id = "citizen@example.com"
    verified = _verified(event_id=raw_event_id)
    normalized = normalize_adapter_event(_payload(), verified)

    assert raw_event_id not in verified.delivery_event_id
    assert raw_event_id not in normalized.ticket_payload["source_event_id"]
    assert normalized.ticket_payload["source_event_id"] == verified.delivery_event_id


def test_contact_identity_is_rejected_instead_of_silently_truncated():
    payload = _payload(
        contact={
            "external_id": "x" * 256,
            "name": "Vecina",
        }
    )
    with pytest.raises(OmnichannelAdapterError) as caught:
        normalize_adapter_event(payload, _verified())

    assert caught.value.code == "invalid_adapter_contact_id"


def test_invalid_persisted_tenant_owner_fails_closed():
    verified = _verified()
    verified.tenant.municipio_id = -1

    with pytest.raises(OmnichannelAdapterError) as caught:
        normalize_adapter_event(_payload(), verified)

    assert caught.value.code == "adapter_tenant_owner_invalid"


@pytest.mark.parametrize(
    "message,error_code",
    [
        (
            {"kind": "location", "location": {"lat": -34.5}},
            "invalid_adapter_location",
        ),
        (
            {
                "kind": "audio",
                "media": [
                    {
                        "provider_media_id": "m-1",
                        "mime_type": "image/png",
                        "size_bytes": 1,
                    }
                ],
            },
            "adapter_media_kind_mismatch",
        ),
        (
            {
                "kind": "image",
                "media": [
                    {
                        "url": "https://127.0.0.1/private.png",
                        "mime_type": "image/png",
                        "size_bytes": 1,
                    }
                ],
            },
            "invalid_adapter_media_url",
        ),
        (
            {
                "kind": "image",
                "media": [
                    {
                        "provider_media_id": "m-1",
                        "mime_type": "image/png",
                        "size_bytes": True,
                    }
                ],
            },
            "invalid_adapter_media_size",
        ),
        (
            {
                "kind": "call_event",
                "call": {"state": "completed", "duration_seconds": 10},
            },
            "missing_adapter_call_id",
        ),
        (
            {
                "kind": "text",
                "text": "hola",
                "call": {"state": "completed", "provider_call_id": "call-1"},
            },
            "unexpected_adapter_call",
        ),
    ],
)
def test_media_location_and_call_contracts_fail_closed(message, error_code):
    with pytest.raises(OmnichannelAdapterError) as caught:
        normalize_adapter_event(_payload(message=message), _verified())

    assert caught.value.code == error_code


def test_invalid_contact_email_is_not_forwarded_to_notification_fields():
    payload = _payload(
        contact={
            "external_id": "provider-user-123",
            "email": "victim@example.com\r\nBcc: attacker@example.com",
        }
    )
    with pytest.raises(OmnichannelAdapterError) as caught:
        normalize_adapter_event(payload, _verified())

    assert caught.value.code == "invalid_adapter_contact_email"


def test_event_reference_is_collision_resistant_and_contains_no_raw_event_id():
    prefix = "evt-" + ("a" * 240)
    left = omni_service._source_event_ref(
        {
            "source": "signed_adapter:telegram",
            "provider_connection_id": 12,
            "source_event_id": prefix + "-left",
        }
    )
    right = omni_service._source_event_ref(
        {
            "source": "signed_adapter:telegram",
            "provider_connection_id": 12,
            "source_event_id": prefix + "-right",
        }
    )

    assert left != right
    assert prefix not in left
    assert prefix not in right
    assert len(left) <= 191
    assert len(right) <= 191


def test_equator_and_greenwich_coordinates_survive_service_normalization():
    assert omni_service._payload_location({"lat": 0.0, "lng": 0.0}) == (
        0.0,
        0.0,
        None,
    )


def test_scoped_placeholder_email_is_deterministic_and_contains_no_pii():
    scoped_id = "omni-v1:" + ("e" * 64)
    first = omni_service._scoped_placeholder_email(scoped_id)
    second = omni_service._scoped_placeholder_email(scoped_id)

    assert first == second
    assert scoped_id not in first
    assert first.endswith("@placeholder.local")


def test_unknown_delivery_claim_outcome_never_reaches_persistence():
    client = _adapter_client()
    verified = _verified()
    normalized = NormalizedAdapterEvent(
        ticket_payload={"mensaje": "Hola"},
        message_kind="text",
        has_media=False,
        awaiting_media_processing=False,
    )
    claim = SimpleNamespace(
        outcome="future_state",
        delivery_id=41,
        attempts=1,
        payload_digest=verified.payload_digest,
        event_type=verified.event_type,
        should_process=False,
    )

    with patch("routes.omnichannel.verify_adapter_request", return_value=verified), patch(
        "routes.omnichannel.parse_adapter_json", return_value={}
    ), patch(
        "routes.omnichannel.normalize_adapter_event", return_value=normalized
    ), patch(
        "routes.omnichannel.claim_verified_adapter_delivery", return_value=claim
    ) as claim_delivery_mock, patch(
        "routes.omnichannel.registrar_interaccion_omnicanal"
    ) as registrar:
        response = client.post(
            "/omnichannel/adapters/12/inbound",
            data=b"{}",
            content_type="application/json",
        )

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "invalid_adapter_delivery_state"
    assert claim_delivery_mock.call_args.args == (verified, b"{}")
    registrar.assert_not_called()


def test_ticket_creation_unavailable_is_retryable_db_error():
    payload = {
        "canal": "telegram",
        "mensaje": "Hola",
        "contacto": {"external_id": "omni-v1:" + ("f" * 64)},
        "identity_contract": "tenant_provider_scoped_v1",
        "tipo_ticket": "municipio",
        "tenant_id": 7,
        "municipio_id": 70,
        "idempotency_key": "omnichannel:7:12:" + ("a" * 64),
    }
    tenant = SimpleNamespace(id=7)
    user = SimpleNamespace(id=9, anon_id=payload["contacto"]["external_id"])
    session = SimpleNamespace(
        get=lambda *_args: tenant,
        rollback=lambda: None,
    )
    fake_db = SimpleNamespace(session=session)

    with patch.object(omni_service, "db", fake_db), patch.object(
        omni_service,
        "normalize_municipio_ticket_write_scope",
        return_value={"tenant_id": 7, "municipio_id": 70},
    ), patch.object(
        omni_service,
        "_deduplicate_contact",
        return_value=user,
    ) as deduplicate_contact, patch.object(
        omni_service,
        "_buscar_ticket_abierto",
        return_value=None,
    ), patch.object(
        omni_service.servicio_tickets,
        "crear_nuevo_ticket",
        return_value=None,
    ):
        result = omni_service.registrar_interaccion_omnicanal(payload)

    assert result == {"exito": False, "motivo": "db_error"}
    assert deduplicate_contact.call_args.kwargs == {
        "identity_mode": "tenant_provider_scoped_v1",
        "tenant": tenant,
    }
