from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import yaml

from config import validate_runtime_security
from routes.omnichannel import omnichannel_bp
from services.omnichannel_adapter_security import (
    CONTRACT_VERSION,
    NormalizedAdapterEvent,
    OmnichannelAdapterError,
    VerifiedAdapterRequest,
    build_adapter_signature,
    derive_connection_signing_secret,
    normalize_adapter_event,
    verify_adapter_request,
)
from services.webhook_delivery_service import CLAIMED, DUPLICATE, IN_PROGRESS


ROOT_SECRET = "omnichannel-root-secret-" + ("x" * 32)
NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def _connection():
    tenant = SimpleNamespace(
        id=7,
        is_active=True,
        municipio_id=70,
        pyme_id=None,
    )
    return SimpleNamespace(
        id=12,
        tenant_id=tenant.id,
        provider="telegram",
        channel="telegram",
        status="active",
        tenant=tenant,
    )


def _body(*, kind="text", message=None, tenant_id=999):
    return json.dumps(
        {
            "contract_version": CONTRACT_VERSION,
            "direction": "inbound",
            "channel": "telegram",
            "event_type": "message.created",
            # These selectors are deliberately untrusted and must be ignored.
            "tenant_id": tenant_id,
            "municipio_id": 9999,
            "contact": {
                "external_id": "provider-user-123",
                "name": "Vecina",
                "email": "vecina@example.com",
                "phone": "+5491112345678",
            },
            "message": message or {"kind": kind, "text": "Hay un árbol caído"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _headers(connection, body):
    timestamp = int(NOW.timestamp())
    event_id = "evt-telegram-001"
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


def test_verified_adapter_derives_tenant_and_scopes_contact_identity():
    connection = _connection()
    body = _body()
    config = {
        "OMNICHANNEL_SIGNED_INBOUND_MODE": "enforce",
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1": ROOT_SECRET,
        "OMNICHANNEL_INBOUND_MAX_PAYLOAD_BYTES": 65536,
        "OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS": 300,
    }
    fake_db = SimpleNamespace(
        session=SimpleNamespace(get=lambda model, object_id: connection)
    )

    with patch("services.omnichannel_adapter_security.db", fake_db):
        verified = verify_adapter_request(
            config=config,
            connection_id=connection.id,
            headers=_headers(connection, body),
            raw_body=body,
            now=NOW,
        )

    normalized = normalize_adapter_event(json.loads(body), verified)
    ticket = normalized.ticket_payload
    assert ticket["tenant_id"] == 7
    assert ticket["municipio_id"] == 70
    assert ticket["tipo_ticket"] == "municipio"
    assert ticket["identity_contract"] == "tenant_provider_scoped_v1"
    assert ticket["contacto"]["external_id"].startswith("omni-v1:")
    assert "provider-user-123" not in ticket["contacto"]["external_id"]


def test_audio_without_transcript_is_accepted_but_explicitly_pending_processing():
    connection = _connection()
    body = _body(
        kind="audio",
        message={
            "kind": "audio",
            "media": [
                {
                    "provider_media_id": "media-1",
                    "mime_type": "audio/ogg",
                    "sha256": "a" * 64,
                    "size_bytes": 3200,
                }
            ],
        },
    )
    verified = VerifiedAdapterRequest(
        connection=connection,
        tenant=connection.tenant,
        provider_namespace="omnichannel:12:telegram",
        event_id="evt-audio-1",
        event_type="message.created",
        timestamp=int(NOW.timestamp()),
        payload_digest="b" * 64,
        connection_secret=derive_connection_signing_secret(ROOT_SECRET, connection),
    )

    normalized = normalize_adapter_event(json.loads(body), verified)

    assert normalized.message_kind == "audio"
    assert normalized.has_media is True
    assert normalized.awaiting_media_processing is True
    assert normalized.ticket_payload["mensaje"] == "Audio recibido; transcripción pendiente."


def test_signature_fails_closed_on_body_tampering_and_stale_timestamp():
    connection = _connection()
    body = _body()
    headers = _headers(connection, body)
    config = {
        "OMNICHANNEL_SIGNED_INBOUND_MODE": "enforce",
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1": ROOT_SECRET,
        "OMNICHANNEL_INBOUND_MAX_CLOCK_SKEW_SECONDS": 300,
    }
    fake_db = SimpleNamespace(
        session=SimpleNamespace(get=lambda model, object_id: connection)
    )

    with patch("services.omnichannel_adapter_security.db", fake_db):
        try:
            verify_adapter_request(
                config=config,
                connection_id=connection.id,
                headers=headers,
                raw_body=body + b" ",
                now=NOW,
            )
        except OmnichannelAdapterError as exc:
            assert exc.code == "invalid_adapter_signature"
        else:  # pragma: no cover - explicit fail-closed assertion
            raise AssertionError("tampered body was accepted")

        stale_headers = dict(headers)
        stale_headers["X-Chatboc-Timestamp"] = str(int(NOW.timestamp()) - 301)
        try:
            verify_adapter_request(
                config=config,
                connection_id=connection.id,
                headers=stale_headers,
                raw_body=body,
                now=NOW,
            )
        except OmnichannelAdapterError as exc:
            assert exc.code == "stale_adapter_request"
        else:  # pragma: no cover
            raise AssertionError("stale signature was accepted")


def _route_client():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        OMNICHANNEL_SIGNED_INBOUND_MODE="enforce",
        OMNICHANNEL_INBOUND_HMAC_SECRET_V1=ROOT_SECRET,
    )
    app.register_blueprint(omnichannel_bp)
    return app.test_client()


def _verified_route_event():
    connection = _connection()
    return VerifiedAdapterRequest(
        connection=connection,
        tenant=connection.tenant,
        provider_namespace="omnichannel:12:telegram",
        event_id="evt-route-1",
        event_type="message.created",
        timestamp=int(NOW.timestamp()),
        payload_digest="d" * 64,
        connection_secret=b"k" * 32,
    )


def _normalized_route_event():
    return NormalizedAdapterEvent(
        ticket_payload={
            "tipo_ticket": "municipio",
            "tenant_id": 7,
            "municipio_id": 70,
            "canal": "telegram",
            "contacto": {"external_id": "omni-v1:" + ("e" * 64)},
            "mensaje": "Hola",
        },
        message_kind="text",
        has_media=False,
        awaiting_media_processing=False,
    )


def _claim(outcome):
    return SimpleNamespace(
        outcome=outcome,
        delivery_id=41,
        attempts=1,
        payload_digest="d" * 64,
        event_type="message.created",
        should_process=outcome == CLAIMED,
    )


def test_route_passes_claim_into_persistence_and_returns_no_tracking_secret():
    client = _route_client()
    verified = _verified_route_event()
    normalized = _normalized_route_event()
    with patch("routes.omnichannel.verify_adapter_request", return_value=verified), patch(
        "routes.omnichannel.parse_adapter_json", return_value={}
    ), patch(
        "routes.omnichannel.normalize_adapter_event", return_value=normalized
    ), patch(
        "routes.omnichannel.claim_verified_adapter_delivery",
        return_value=_claim(CLAIMED),
    ), patch(
        "routes.omnichannel.registrar_interaccion_omnicanal",
        return_value={"exito": True, "nuevo_ticket": True, "ticket_id": 99},
    ) as registrar:
        response = client.post(
            "/omnichannel/adapters/12/inbound",
            data=b"{}",
            content_type="application/json",
        )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["status"] == "accepted"
    assert payload["ticket"] == {"id": 99, "created": True, "source_model": None}
    assert "consulta_pin" not in json.dumps(payload)
    registrar.assert_called_once()
    assert registrar.call_args.kwargs["delivery_claim"].delivery_id == 41


def test_route_duplicate_and_in_progress_never_persist_again():
    client = _route_client()
    verified = _verified_route_event()
    normalized = _normalized_route_event()
    for outcome, expected_status in ((DUPLICATE, 200), (IN_PROGRESS, 202)):
        with patch("routes.omnichannel.verify_adapter_request", return_value=verified), patch(
            "routes.omnichannel.parse_adapter_json", return_value={}
        ), patch(
            "routes.omnichannel.normalize_adapter_event", return_value=normalized
        ), patch(
            "routes.omnichannel.claim_verified_adapter_delivery",
            return_value=_claim(outcome),
        ), patch("routes.omnichannel.registrar_interaccion_omnicanal") as registrar:
            response = client.post(
                "/omnichannel/adapters/12/inbound",
                data=b"{}",
                content_type="application/json",
            )
        assert response.status_code == expected_status
        registrar.assert_not_called()


def test_route_claim_loss_uses_retryable_service_unavailable_status():
    client = _route_client()
    verified = _verified_route_event()
    normalized = _normalized_route_event()
    with patch("routes.omnichannel.verify_adapter_request", return_value=verified), patch(
        "routes.omnichannel.parse_adapter_json", return_value={}
    ), patch(
        "routes.omnichannel.normalize_adapter_event", return_value=normalized
    ), patch(
        "routes.omnichannel.claim_verified_adapter_delivery",
        return_value=_claim(CLAIMED),
    ), patch(
        "routes.omnichannel.registrar_interaccion_omnicanal",
        return_value={"exito": False, "motivo": "delivery_claim_lost"},
    ), patch("routes.omnichannel.fail_delivery") as fail_delivery:
        response = client.post(
            "/omnichannel/adapters/12/inbound",
            data=b"{}",
            content_type="application/json",
        )

    assert response.status_code == 503
    assert response.get_json()["retryable"] is True
    fail_delivery.assert_called_once_with(41, 1, "delivery_claim_lost")


def test_production_config_keeps_signed_adapter_fail_closed():
    base = {
        "ENV": "production",
        "DEBUG": False,
        "SECRET_KEY": "s" * 32,
        "WHATSAPP_INBOUND_DURABILITY_MODE": "legacy",
        "CHANNEL_SESSION_IDENTITY_MODE": "legacy",
        "CHANNEL_SESSION_IDENTITY_VERSION_V1": "v1",
        "OMNICHANNEL_SIGNED_INBOUND_MODE": "enforce",
        "OMNICHANNEL_INBOUND_HMAC_SECRET_V1": "short",
    }
    errors = validate_runtime_security(base)
    assert any("OMNICHANNEL_INBOUND_HMAC_SECRET_V1" in error for error in errors)

    base["OMNICHANNEL_INBOUND_HMAC_SECRET_V1"] = ROOT_SECRET
    errors = validate_runtime_security(base)
    assert not any("OMNICHANNEL_" in error for error in errors)


def test_render_declares_adapter_disabled_and_secret_unset():
    manifest = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
    services = {service["name"]: service for service in manifest["services"]}
    web_env = {item["key"]: item for item in services["chatboc-backend"]["envVars"]}

    assert web_env["OMNICHANNEL_SIGNED_INBOUND_MODE"]["value"] == "disabled"
    assert web_env["OMNICHANNEL_INBOUND_HMAC_SECRET_V1"] == {
        "key": "OMNICHANNEL_INBOUND_HMAC_SECRET_V1",
        "sync": False,
    }
