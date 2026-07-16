import base64
import hashlib
import hmac
import itertools
import json
import logging

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from flask import Flask
import pytest

import services.meta_flow_data_exchange as flow_service
from routes.meta_flow_data_exchange import create_meta_flow_data_exchange_blueprint
from services.meta_flow_data_exchange import MetaFlowEndpointConfig, endpoint_config_readiness


ENDPOINT_ID = "waba-test-001"
TENANT_ID = "tenant-test-001"
PATH = f"/api/whatsapp/flows/data-exchange/{ENDPOINT_ID}"
APP_SECRET = b"meta-flow-test-app-secret"
AES_KEY = bytes(range(16))
INITIAL_VECTOR = bytes(range(16, 32))
_BLUEPRINT_SEQUENCE = itertools.count()


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def private_key_pem(private_key):
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _make_client(
    private_key_pem,
    *,
    handlers=None,
    app_secret=APP_SECRET,
    max_payload_bytes=64 * 1024,
):
    config = MetaFlowEndpointConfig(
        endpoint_id=ENDPOINT_ID,
        tenant_id=TENANT_ID,
        waba_id=ENDPOINT_ID,
        private_key_pem=private_key_pem,
        app_secret=app_secret,
        handlers=handlers or {},
        max_payload_bytes=max_payload_bytes,
    )

    def resolver(endpoint_id):
        return config if endpoint_id == ENDPOINT_ID else None

    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(
        create_meta_flow_data_exchange_blueprint(
            config_resolver=resolver,
            name=f"meta_flow_data_exchange_test_{next(_BLUEPRINT_SEQUENCE)}",
        )
    )
    return app.test_client(), config


def _encrypted_request(public_key, payload):
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encryptor = Cipher(
        algorithms.AES(AES_KEY),
        modes.GCM(INITIAL_VECTOR),
    ).encryptor()
    encrypted_flow_data = encryptor.update(plaintext) + encryptor.finalize() + encryptor.tag
    encrypted_aes_key = public_key.encrypt(
        AES_KEY,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    envelope = {
        "encrypted_aes_key": base64.b64encode(encrypted_aes_key).decode("ascii"),
        "encrypted_flow_data": base64.b64encode(encrypted_flow_data).decode("ascii"),
        "initial_vector": base64.b64encode(INITIAL_VECTOR).decode("ascii"),
    }
    return json.dumps(envelope, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _signature(raw_body, secret=APP_SECRET):
    return "sha256=" + hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def _post(client, raw_body, signature=None):
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(PATH, data=raw_body, headers=headers)


def _decrypt_response(response):
    encrypted = base64.b64decode(response.get_data(as_text=True), validate=True)
    flipped_iv = bytes(byte ^ 0xFF for byte in INITIAL_VECTOR)
    decryptor = Cipher(
        algorithms.AES(AES_KEY),
        modes.GCM(flipped_iv, encrypted[-16:]),
    ).decryptor()
    plaintext = decryptor.update(encrypted[:-16]) + decryptor.finalize()
    return json.loads(plaintext.decode("utf-8"))


def test_endpoint_readiness_validates_private_key_and_signature(private_key_pem):
    configured = MetaFlowEndpointConfig(
        endpoint_id=ENDPOINT_ID,
        tenant_id=TENANT_ID,
        waba_id=ENDPOINT_ID,
        private_key_pem=private_key_pem,
        app_secret=APP_SECRET,
        handlers={},
    )
    unsigned = MetaFlowEndpointConfig(
        endpoint_id=ENDPOINT_ID,
        tenant_id=TENANT_ID,
        waba_id=ENDPOINT_ID,
        private_key_pem=private_key_pem,
        handlers={},
    )
    invalid_key = MetaFlowEndpointConfig(
        endpoint_id=ENDPOINT_ID,
        tenant_id=TENANT_ID,
        waba_id=ENDPOINT_ID,
        private_key_pem=b"not-a-private-key",
        app_secret=APP_SECRET,
        handlers={},
    )

    assert endpoint_config_readiness(configured)["ready"] is True
    assert endpoint_config_readiness(unsigned)["failure_code"] == "app_secret_not_configured"
    assert endpoint_config_readiness(invalid_key)["failure_code"] == "private_key_invalid"


def test_rejects_invalid_raw_body_signature(private_key, private_key_pem):
    client, _ = _make_client(private_key_pem)
    raw_body = _encrypted_request(
        private_key.public_key(),
        {"version": "3.0", "action": "ping"},
    )

    response = _post(client, raw_body, "sha256=" + "00" * 32)

    assert response.status_code == 432
    assert response.get_json()["error"]["code"] == "invalid_signature"
    assert response.headers["Cache-Control"] == "no-store"


def test_returns_421_when_rsa_key_does_not_match(private_key_pem):
    other_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    client, _ = _make_client(private_key_pem)
    raw_body = _encrypted_request(
        other_private_key.public_key(),
        {"version": "3.0", "action": "ping"},
    )

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 421
    error = response.get_json()["error"]
    assert error["code"] == "decryption_failed"
    assert "key" not in json.dumps(error).lower()


def test_ping_returns_encrypted_active_status(private_key, private_key_pem):
    client, _ = _make_client(private_key_pem)
    raw_body = _encrypted_request(
        private_key.public_key(),
        {"version": "3.0", "action": "PING"},
    )

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 200
    assert response.content_type == "text/plain; charset=utf-8"
    assert _decrypt_response(response) == {"data": {"status": "active"}}


def test_init_uses_tenant_bound_handler(private_key, private_key_pem):
    observed = {}

    def handle_init(payload, context):
        observed["payload"] = payload
        observed["context"] = context
        return {"screen": "WELCOME", "data": {"ready": True}}

    client, _ = _make_client(private_key_pem, handlers={"INIT": handle_init})
    payload = {
        "version": "3.0",
        "action": "INIT",
        "flow_token": "opaque-test-token",
        "data": {},
    }
    raw_body = _encrypted_request(private_key.public_key(), payload)

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 200
    assert _decrypt_response(response) == {
        "screen": "WELCOME",
        "data": {"ready": True},
    }
    assert observed["payload"]["flow_token"] == "opaque-test-token"
    assert observed["context"].tenant_id == TENANT_ID
    assert observed["context"].waba_id == ENDPOINT_ID
    assert observed["context"].action == "init"


def test_back_uses_explicit_handler(private_key, private_key_pem):
    def handle_back(payload, context):
        assert context.action == "back"
        return {"screen": "PREVIOUS", "data": {"from": payload["screen"]}}

    client, _ = _make_client(private_key_pem, handlers={"back": handle_back})
    raw_body = _encrypted_request(
        private_key.public_key(),
        {
            "version": "3.0",
            "action": "BACK",
            "screen": "CURRENT",
            "data": {},
        },
    )

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 200
    assert _decrypt_response(response) == {
        "screen": "PREVIOUS",
        "data": {"from": "CURRENT"},
    }


def test_data_exchange_uses_screen_handler(private_key, private_key_pem):
    def handle_exchange(payload, context):
        assert context.action == "data_exchange"
        return {
            "screen": "REVIEW",
            "data": {"selected_id": payload["data"]["selected_id"]},
        }

    client, _ = _make_client(
        private_key_pem,
        handlers={"data_exchange": handle_exchange},
    )
    raw_body = _encrypted_request(
        private_key.public_key(),
        {
            "version": "3.0",
            "action": "data_exchange",
            "screen": "FORM",
            "flow_token": "opaque-test-token",
            "data": {"selected_id": "item-42"},
        },
    )

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 200
    assert _decrypt_response(response) == {
        "screen": "REVIEW",
        "data": {"selected_id": "item-42"},
    }


def test_error_notification_is_acknowledged_without_logging_payload(
    private_key,
    private_key_pem,
    caplog,
):
    def handle_error(payload, context):
        assert context.action == "error"
        assert payload["data"]["error"] == "client_validation_failed"
        return {"data": {"acknowledged": True}}

    client, config = _make_client(private_key_pem, handlers={"error": handle_error})
    raw_body = _encrypted_request(
        private_key.public_key(),
        {
            "version": "3.0",
            "action": "data_exchange",
            "screen": "FORM",
            "data": {
                "error": "client_validation_failed",
                "email": "private-person@example.test",
            },
        },
    )

    with caplog.at_level(logging.INFO):
        response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 200
    assert _decrypt_response(response) == {"data": {"acknowledged": True}}
    assert "private-person@example.test" not in caplog.text
    assert APP_SECRET.decode("ascii") not in caplog.text
    assert "BEGIN PRIVATE KEY" not in caplog.text
    assert "BEGIN PRIVATE KEY" not in repr(config)
    assert APP_SECRET.decode("ascii") not in repr(config)


def test_rejects_payload_over_endpoint_limit(private_key_pem):
    client, _ = _make_client(private_key_pem, max_payload_bytes=1024)
    raw_body = b"{" + b"x" * 1024

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "payload_too_large"


def test_signature_is_optional_only_when_no_app_secret(private_key, private_key_pem):
    client, _ = _make_client(private_key_pem, app_secret=None)
    raw_body = _encrypted_request(
        private_key.public_key(),
        {"version": "3.0", "action": "ping"},
    )

    response = _post(client, raw_body)

    assert response.status_code == 200
    assert _decrypt_response(response) == {"data": {"status": "active"}}


def test_fails_closed_when_cryptography_is_unavailable(
    private_key,
    private_key_pem,
    monkeypatch,
):
    client, _ = _make_client(private_key_pem)
    raw_body = _encrypted_request(
        private_key.public_key(),
        {"version": "3.0", "action": "ping"},
    )
    monkeypatch.setattr(flow_service, "_CRYPTOGRAPHY_AVAILABLE", False)

    response = _post(client, raw_body, _signature(raw_body))

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "cryptography_unavailable"
