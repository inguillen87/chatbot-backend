"""Security and dispatch primitives for Meta WhatsApp Flows Data Exchange."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import inspect
import json
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only in broken deployments
    hashes = serialization = padding = rsa = None  # type: ignore[assignment]
    Cipher = algorithms = modes = None  # type: ignore[assignment]
    _CRYPTOGRAPHY_AVAILABLE = False


DATA_API_VERSION = "3.0"
AES_KEY_BYTES = 16
GCM_TAG_BYTES = 16
INITIAL_VECTOR_BYTES = 16
DEFAULT_MAX_PAYLOAD_BYTES = 64 * 1024
DEFAULT_MAX_DECRYPTED_BYTES = 64 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 64 * 1024
HARD_MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_RSA_CIPHERTEXT_BYTES = 1024
MAX_JSON_DEPTH = 24
MAX_JSON_NODES = 4096

_ENDPOINT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SIGNATURE = re.compile(r"^sha256=([0-9a-fA-F]{64})$")
_SUPPORTED_ACTIONS = frozenset({"ping", "init", "back", "data_exchange", "error"})


class MetaFlowEndpointError(Exception):
    """An endpoint failure with a stable, non-sensitive public representation."""

    def __init__(self, code: str, safe_message: str, status_code: int) -> None:
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code
        super().__init__(code)


class MetaFlowConfigurationError(MetaFlowEndpointError):
    def __init__(self, code: str = "endpoint_configuration_invalid") -> None:
        super().__init__(code, "Flow endpoint configuration is unavailable.", 503)


class MetaFlowCryptoUnavailableError(MetaFlowEndpointError):
    def __init__(self) -> None:
        super().__init__(
            "cryptography_unavailable",
            "Flow endpoint cryptography is unavailable.",
            503,
        )


class MetaFlowValidationError(MetaFlowEndpointError):
    def __init__(self, code: str, safe_message: str = "Invalid Flow request payload.") -> None:
        super().__init__(code, safe_message, 400)


class MetaFlowDecryptionError(MetaFlowEndpointError):
    def __init__(self) -> None:
        # Meta reserves 421 for request decryption/key failures and may retry
        # after refreshing the uploaded public key.
        super().__init__("decryption_failed", "Unable to decrypt Flow request.", 421)


class MetaFlowActionError(MetaFlowEndpointError):
    pass


class MetaFlowResponseError(MetaFlowEndpointError):
    def __init__(self, code: str = "response_encryption_failed") -> None:
        super().__init__(code, "Unable to produce Flow response.", 500)


@dataclass(frozen=True)
class MetaFlowRequestContext:
    """Non-secret request metadata exposed to business handlers."""

    request_id: str
    endpoint_id: str
    tenant_id: str
    waba_id: str
    action: str


FlowActionHandler = Callable[
    [Mapping[str, Any], MetaFlowRequestContext],
    Mapping[str, Any],
]


@dataclass(frozen=True, repr=False)
class MetaFlowEndpointConfig:
    """Tenant/WABA-bound endpoint configuration with redacted repr output."""

    endpoint_id: str
    tenant_id: str | int
    waba_id: str | int
    private_key_pem: str | bytes | None = field(repr=False)
    private_key_passphrase: str | bytes | None = field(default=None, repr=False)
    app_secret: str | bytes | None = field(default=None, repr=False)
    previous_app_secret: str | bytes | None = field(default=None, repr=False)
    handlers: Mapping[str, FlowActionHandler] = field(default_factory=dict, repr=False)
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES
    max_decrypted_bytes: int = DEFAULT_MAX_DECRYPTED_BYTES
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        endpoint_id = _validated_identifier(self.endpoint_id, "endpoint_id")
        tenant_id = _validated_identifier(self.tenant_id, "tenant_id")
        waba_id = _validated_identifier(self.waba_id, "waba_id")
        normalized_handlers: dict[str, FlowActionHandler] = {}
        if not isinstance(self.handlers, Mapping):
            raise MetaFlowConfigurationError("handlers_invalid")
        for action, handler in self.handlers.items():
            normalized_action = normalize_action(action)
            if normalized_action not in _SUPPORTED_ACTIONS or not callable(handler):
                raise MetaFlowConfigurationError("handlers_invalid")
            normalized_handlers[normalized_action] = handler

        # Validate secret types now, but retain their original values so PEM
        # and passphrase handling remains compatible with cryptography.
        _optional_secret_bytes(self.app_secret, "app_secret_invalid")
        _optional_secret_bytes(self.previous_app_secret, "app_secret_invalid")
        _optional_secret_bytes(self.private_key_passphrase, "private_key_invalid")

        object.__setattr__(self, "endpoint_id", endpoint_id)
        object.__setattr__(self, "tenant_id", tenant_id)
        object.__setattr__(self, "waba_id", waba_id)
        object.__setattr__(self, "handlers", MappingProxyType(normalized_handlers))
        object.__setattr__(
            self,
            "max_payload_bytes",
            _bounded_limit(self.max_payload_bytes, DEFAULT_MAX_PAYLOAD_BYTES, 1024),
        )
        object.__setattr__(
            self,
            "max_decrypted_bytes",
            _bounded_limit(self.max_decrypted_bytes, DEFAULT_MAX_DECRYPTED_BYTES, 256),
        )
        object.__setattr__(
            self,
            "max_response_bytes",
            _bounded_limit(self.max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES, 256),
        )

    def __repr__(self) -> str:
        return (
            "MetaFlowEndpointConfig("
            f"endpoint_id={self.endpoint_id!r}, "
            f"tenant_id={self.tenant_id!r}, "
            f"waba_id={self.waba_id!r}, "
            f"signature_enabled={self.signature_enabled}, "
            f"handlers={sorted(self.handlers)!r})"
        )

    @property
    def signature_enabled(self) -> bool:
        return bool(self.signature_secrets())

    def signature_secrets(self) -> tuple[bytes, ...]:
        values = (
            _optional_secret_bytes(self.app_secret, "app_secret_invalid"),
            _optional_secret_bytes(self.previous_app_secret, "app_secret_invalid"),
        )
        return tuple(value for value in values if value)


@dataclass(frozen=True, repr=False)
class DecryptedMetaFlowRequest:
    payload: Mapping[str, Any]
    aes_key: bytes = field(repr=False)
    initial_vector: bytes = field(repr=False)

    def __repr__(self) -> str:
        action = normalize_action(self.payload.get("action"))
        return f"DecryptedMetaFlowRequest(action={action!r})"


def cryptography_available() -> bool:
    return _CRYPTOGRAPHY_AVAILABLE


def endpoint_config_readiness(config: MetaFlowEndpointConfig) -> dict[str, Any]:
    """Validate endpoint crypto material without exposing or serializing secrets."""

    if not isinstance(config, MetaFlowEndpointConfig):
        raise TypeError("config must be MetaFlowEndpointConfig")
    if not _CRYPTOGRAPHY_AVAILABLE:
        return {
            "ready": False,
            "private_key_ready": False,
            "signature_ready": config.signature_enabled,
            "failure_code": "cryptography_unavailable",
        }
    try:
        private_key_bytes = _private_key_bytes(config.private_key_pem)
        password = _optional_secret_bytes(
            config.private_key_passphrase,
            "private_key_invalid",
        )
        private_key = serialization.load_pem_private_key(
            private_key_bytes,
            password=password,
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("not_rsa")
        if private_key.key_size < 2048 or private_key.key_size > 8192:
            raise ValueError("invalid_rsa_size")
    except Exception:
        return {
            "ready": False,
            "private_key_ready": False,
            "signature_ready": config.signature_enabled,
            "failure_code": "private_key_invalid",
        }
    signature_ready = config.signature_enabled
    return {
        "ready": bool(signature_ready),
        "private_key_ready": True,
        "signature_ready": signature_ready,
        "failure_code": None if signature_ready else "app_secret_not_configured",
    }


def normalize_action(value: Any) -> str:
    return str(value or "").strip().casefold()


def endpoint_fingerprint(endpoint_id: Any) -> str:
    """Return a log-safe endpoint correlation value, not the raw identifier."""

    return hashlib.sha256(str(endpoint_id or "").encode("utf-8")).hexdigest()[:12]


def coerce_endpoint_config(
    endpoint_id: str,
    value: MetaFlowEndpointConfig | Mapping[str, Any],
) -> MetaFlowEndpointConfig:
    """Validate resolver output without ever rendering secret values."""

    normalized_endpoint_id = _validated_identifier(endpoint_id, "endpoint_id")
    if isinstance(value, MetaFlowEndpointConfig):
        if not hmac.compare_digest(value.endpoint_id, normalized_endpoint_id):
            raise MetaFlowConfigurationError("endpoint_scope_mismatch")
        return value
    if not isinstance(value, Mapping):
        raise MetaFlowConfigurationError("endpoint_configuration_invalid")

    configured_endpoint = str(value.get("endpoint_id") or normalized_endpoint_id).strip()
    if not hmac.compare_digest(configured_endpoint, normalized_endpoint_id):
        raise MetaFlowConfigurationError("endpoint_scope_mismatch")
    try:
        return MetaFlowEndpointConfig(
            endpoint_id=normalized_endpoint_id,
            tenant_id=value.get("tenant_id"),
            waba_id=value.get("waba_id") or normalized_endpoint_id,
            private_key_pem=value.get("private_key_pem", value.get("private_key")),
            private_key_passphrase=value.get(
                "private_key_passphrase",
                value.get("passphrase"),
            ),
            app_secret=value.get("app_secret"),
            previous_app_secret=value.get("previous_app_secret"),
            handlers=value.get("handlers") or {},
            max_payload_bytes=value.get("max_payload_bytes", DEFAULT_MAX_PAYLOAD_BYTES),
            max_decrypted_bytes=value.get(
                "max_decrypted_bytes",
                DEFAULT_MAX_DECRYPTED_BYTES,
            ),
            max_response_bytes=value.get("max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES),
        )
    except MetaFlowEndpointError:
        raise
    except (TypeError, ValueError) as exc:
        raise MetaFlowConfigurationError("endpoint_configuration_invalid") from exc


def is_request_signature_valid(
    raw_body: bytes,
    signature_header: str | None,
    config: MetaFlowEndpointConfig,
) -> bool:
    """Verify Meta's SHA-256 HMAC over the exact raw HTTP request bytes."""

    secrets = config.signature_secrets()
    if not secrets:
        return True
    if not isinstance(raw_body, bytes) or not isinstance(signature_header, str):
        return False
    match = _SIGNATURE.fullmatch(signature_header.strip())
    if match is None:
        return False
    supplied = match.group(1).lower()
    return any(
        hmac.compare_digest(
            hmac.new(secret, raw_body, hashlib.sha256).hexdigest(),
            supplied,
        )
        for secret in secrets
    )


def parse_encrypted_envelope(raw_body: bytes) -> Mapping[str, str]:
    envelope = _load_json_object(raw_body, "invalid_envelope")
    required = ("encrypted_flow_data", "encrypted_aes_key", "initial_vector")
    result: dict[str, str] = {}
    for field_name in required:
        value = envelope.get(field_name)
        if not isinstance(value, str) or not value or len(value) > HARD_MAX_PAYLOAD_BYTES * 2:
            raise MetaFlowValidationError("invalid_envelope")
        result[field_name] = value
    return MappingProxyType(result)


def decrypt_flow_request(
    envelope: Mapping[str, str],
    config: MetaFlowEndpointConfig,
) -> DecryptedMetaFlowRequest:
    """Decrypt a Data API 3.0 request and authenticate its GCM tag."""

    _require_cryptography()
    private_key_pem = _private_key_bytes(config.private_key_pem)
    passphrase = _optional_secret_bytes(
        config.private_key_passphrase,
        "private_key_invalid",
    )
    encrypted_aes_key = _strict_b64decode(
        envelope.get("encrypted_aes_key"),
        max_decoded_bytes=MAX_RSA_CIPHERTEXT_BYTES,
    )
    encrypted_flow_data = _strict_b64decode(
        envelope.get("encrypted_flow_data"),
        max_decoded_bytes=config.max_decrypted_bytes + GCM_TAG_BYTES,
    )
    initial_vector = _strict_b64decode(
        envelope.get("initial_vector"),
        max_decoded_bytes=INITIAL_VECTOR_BYTES,
    )
    if len(initial_vector) != INITIAL_VECTOR_BYTES:
        raise MetaFlowDecryptionError()
    if len(encrypted_flow_data) <= GCM_TAG_BYTES:
        raise MetaFlowDecryptionError()

    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem,
            password=passphrase,
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("not_rsa")
        if private_key.key_size < 2048 or private_key.key_size > 8192:
            raise ValueError("invalid_rsa_size")
        if len(encrypted_aes_key) != private_key.key_size // 8:
            raise ValueError("invalid_rsa_ciphertext")
        aes_key = private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        if len(aes_key) != AES_KEY_BYTES:
            raise ValueError("invalid_aes_key")

        ciphertext = encrypted_flow_data[:-GCM_TAG_BYTES]
        authentication_tag = encrypted_flow_data[-GCM_TAG_BYTES:]
        decryptor = Cipher(
            algorithms.AES(aes_key),
            modes.GCM(initial_vector, authentication_tag),
        ).decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()
    except Exception as exc:
        raise MetaFlowDecryptionError() from exc

    if len(plaintext) > config.max_decrypted_bytes:
        raise MetaFlowValidationError("decrypted_payload_too_large")
    payload = _load_json_object(plaintext, "invalid_decrypted_payload")
    _validate_json_complexity(payload)
    return DecryptedMetaFlowRequest(
        payload=MappingProxyType(payload),
        aes_key=aes_key,
        initial_vector=initial_vector,
    )


def dispatch_flow_request(
    payload: Mapping[str, Any],
    config: MetaFlowEndpointConfig,
    *,
    request_id: str,
) -> Mapping[str, Any]:
    """Dispatch only Data API 3.0 actions; business success needs a handler."""

    if payload.get("version") != DATA_API_VERSION:
        raise MetaFlowActionError(
            "unsupported_data_api_version",
            "Unsupported Flow Data API version.",
            400,
        )
    action = normalize_action(payload.get("action"))
    if action not in _SUPPORTED_ACTIONS - {"error"}:
        raise MetaFlowActionError(
            "unsupported_action",
            "Unsupported Flow action.",
            400,
        )

    data = payload.get("data")
    is_error_notification = (
        isinstance(data, Mapping)
        and "error" in data
        and data.get("error") is not None
    )
    dispatch_action = "error" if action != "ping" and is_error_notification else action
    context = MetaFlowRequestContext(
        request_id=request_id,
        endpoint_id=config.endpoint_id,
        tenant_id=config.tenant_id,
        waba_id=config.waba_id,
        action=dispatch_action,
    )

    handler = config.handlers.get(dispatch_action)
    if handler is None:
        if dispatch_action == "ping":
            return {"data": {"status": "active"}}
        if dispatch_action == "error":
            return {"data": {"acknowledged": True}}
        raise MetaFlowActionError(
            "handler_not_configured",
            "Flow action handler is not configured.",
            500,
        )

    try:
        response = handler(payload, context)
    except MetaFlowEndpointError:
        raise
    except Exception as exc:
        raise MetaFlowActionError(
            "handler_failed",
            "Flow action could not be completed.",
            500,
        ) from exc
    if inspect.isawaitable(response) or not isinstance(response, Mapping):
        raise MetaFlowActionError(
            "handler_response_invalid",
            "Flow action returned an invalid response.",
            500,
        )
    return dict(response)


def encrypt_flow_response(
    response: Mapping[str, Any],
    decrypted_request: DecryptedMetaFlowRequest,
    *,
    max_response_bytes: int,
) -> str:
    """Encrypt a response with AES-GCM and the bitwise-inverted request IV."""

    _require_cryptography()
    if not isinstance(response, Mapping):
        raise MetaFlowResponseError("handler_response_invalid")
    try:
        response_bytes = json.dumps(
            dict(response),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MetaFlowResponseError("handler_response_invalid") from exc
    if len(response_bytes) > max_response_bytes:
        raise MetaFlowResponseError("handler_response_too_large")
    if (
        len(decrypted_request.aes_key) != AES_KEY_BYTES
        or len(decrypted_request.initial_vector) != INITIAL_VECTOR_BYTES
    ):
        raise MetaFlowResponseError()

    flipped_iv = bytes(byte ^ 0xFF for byte in decrypted_request.initial_vector)
    try:
        encryptor = Cipher(
            algorithms.AES(decrypted_request.aes_key),
            modes.GCM(flipped_iv),
        ).encryptor()
        ciphertext = encryptor.update(response_bytes) + encryptor.finalize()
        return base64.b64encode(ciphertext + encryptor.tag).decode("ascii")
    except Exception as exc:
        raise MetaFlowResponseError() from exc


def build_encrypted_error_payload(error: MetaFlowEndpointError, request_id: str) -> dict[str, Any]:
    return {
        "data": {
            "error": {
                "code": error.code,
                "message": error.safe_message,
                "request_id": request_id,
            }
        }
    }


def _require_cryptography() -> None:
    if not _CRYPTOGRAPHY_AVAILABLE:
        raise MetaFlowCryptoUnavailableError()


def _validated_identifier(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not _ENDPOINT_ID.fullmatch(normalized):
        raise MetaFlowConfigurationError(f"{field_name}_invalid")
    return normalized


def _bounded_limit(value: Any, default: int, minimum: int) -> int:
    try:
        normalized = int(value if value is not None else default)
    except (TypeError, ValueError) as exc:
        raise MetaFlowConfigurationError("payload_limits_invalid") from exc
    if normalized < minimum or normalized > HARD_MAX_PAYLOAD_BYTES:
        raise MetaFlowConfigurationError("payload_limits_invalid")
    return normalized


def _optional_secret_bytes(value: Any, error_code: str) -> bytes | None:
    if value is None or value == "" or value == b"":
        return None
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    raise MetaFlowConfigurationError(error_code)


def _private_key_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        encoded = bytes(value)
    else:
        raise MetaFlowDecryptionError()
    if not encoded or len(encoded) > MAX_PRIVATE_KEY_BYTES:
        raise MetaFlowDecryptionError()
    return encoded


def _strict_b64decode(value: Any, *, max_decoded_bytes: int) -> bytes:
    if not isinstance(value, str) or not value:
        raise MetaFlowDecryptionError()
    max_encoded_bytes = ((max_decoded_bytes + 2) // 3) * 4
    if len(value) > max_encoded_bytes:
        raise MetaFlowDecryptionError()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MetaFlowDecryptionError() from exc
    if len(decoded) > max_decoded_bytes:
        raise MetaFlowDecryptionError()
    return decoded


class _DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _reject_non_finite_number(value: str) -> None:
    raise ValueError(value)


def _load_json_object(raw: bytes, error_code: str) -> dict[str, Any]:
    if not isinstance(raw, bytes) or not raw:
        raise MetaFlowValidationError(error_code)
    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeError, json.JSONDecodeError, _DuplicateJsonKey, ValueError) as exc:
        raise MetaFlowValidationError(error_code) from exc
    if not isinstance(value, dict):
        raise MetaFlowValidationError(error_code)
    return value


def _validate_json_complexity(value: Any) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    node_count = 0
    while stack:
        current, depth = stack.pop()
        node_count += 1
        if depth > MAX_JSON_DEPTH or node_count > MAX_JSON_NODES:
            raise MetaFlowValidationError("decrypted_payload_too_complex")
        if isinstance(current, Mapping):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
