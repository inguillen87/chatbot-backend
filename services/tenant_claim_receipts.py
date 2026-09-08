"""Secure intake receipts for public ``TenantTicket`` claims.

The public tracking PIN is deliberately derived at response time and is never
persisted.  A versioned, dedicated HMAC secret provides a rotation boundary
that is independent from Flask sessions and every other signing domain.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import re
import unicodedata
from typing import Any

from flask import current_app

from models import TenantTicket
from utils.time_utils import datetime_to_iso_utc


TENANT_CLAIM_INTAKE_RECEIPT_CONTRACT_VERSION = "claims.intake_receipt.v1"
TENANT_CLAIM_RECEIPT_SECRET_VERSION = "v1"
TENANT_CLAIM_RECEIPT_SECRET_CONFIGS = {
    "v1": "TENANT_CLAIM_RECEIPT_SECRET_V1",
}

_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{8,128}$")
_MAX_DESCRIPTION_LENGTH = 5_000
_MAX_CATEGORY_LENGTH = 80
_MAX_EXTRAS_BYTES = 16 * 1024
_MAX_EXTRAS_DEPTH = 10
_MAX_EXTRAS_ITEMS = 512
_MIN_SECRET_BYTES = 32
_RESERVED_EXTRAS_KEYS = {
    "assignee_id", "assignee_name", "assignee_email", "assigned_user_id", "assigned_agent_id",
    "assigned_to", "asignado_a_id", "handoff", "handoff_state", "closed_by", "closed_at",
    "reopened_by", "reopened_at", "timeline", "events", "comments", "handoff_history",
    "access_pin",
    "claim_pin",
    "consulta_pin",
    "idempotency_key",
    "intake_idempotency_hash",
    "intake_payload_hash",
    "pin",
    "receipt_pin",
    "tracking_pin",
}


class TenantClaimValidationError(ValueError):
    """A public claim request failed deterministic input validation."""

    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.public_message = message


class TenantClaimReceiptSecretUnavailable(RuntimeError):
    """The dedicated receipt key is missing, weak, or unsupported."""


@dataclass(frozen=True)
class NormalizedTenantClaim:
    descripcion: str
    categoria: str | None
    latitud: float | None
    longitud: float | None
    extras: dict[str, Any]
    canonical_payload: str
    payload_hash: str


def _nfc(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise _validation_error(
            "claim_payload_invalid",
            "El reclamo contiene caracteres no validos.",
        ) from exc
    return normalized


def _validation_error(reason_code: str, message: str) -> TenantClaimValidationError:
    return TenantClaimValidationError(reason_code, message)


def normalize_idempotency_key(value: Any, *, required: bool) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise _validation_error(
                "idempotency_key_required",
                "Idempotency-Key es requerido para registrar el reclamo.",
            )
        return None
    if not isinstance(value, str):
        raise _validation_error(
            "idempotency_key_invalid",
            "Idempotency-Key no tiene un formato valido.",
        )
    normalized = value.strip()
    if not _IDEMPOTENCY_KEY_PATTERN.fullmatch(normalized):
        raise _validation_error(
            "idempotency_key_invalid",
            "Idempotency-Key no tiene un formato valido.",
        )
    return normalized


def _normalize_text(
    value: Any,
    *,
    field: str,
    required: bool,
    max_length: int,
) -> str | None:
    if value is None:
        if required:
            raise _validation_error(
                f"{field}_required",
                f"{field} es requerido.",
            )
        return None
    if not isinstance(value, str):
        raise _validation_error(
            f"{field}_invalid",
            f"{field} debe ser texto.",
        )
    normalized = _nfc(value.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if "\x00" in normalized:
        raise _validation_error(
            f"{field}_invalid",
            f"{field} contiene caracteres no validos.",
        )
    if not normalized:
        if required:
            raise _validation_error(
                f"{field}_required",
                f"{field} es requerido.",
            )
        return None
    if len(normalized) > max_length:
        raise _validation_error(
            f"{field}_too_long",
            f"{field} supera el largo permitido.",
        )
    return normalized


def _normalize_coordinate(value: Any, *, field: str, lower: float, upper: float) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise _validation_error(
            "coordinates_invalid",
            "Las coordenadas no son validas.",
        )
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _validation_error(
            "coordinates_invalid",
            "Las coordenadas no son validas.",
        ) from exc
    if not math.isfinite(normalized) or not lower <= normalized <= upper:
        raise _validation_error(
            "coordinates_invalid",
            "Las coordenadas no son validas.",
        )
    # Canonicalize negative zero so semantically identical retries hash equally.
    return 0.0 if normalized == 0 else normalized


def _normalize_extra_value(value: Any, *, depth: int, counter: list[int]) -> Any:
    if depth > _MAX_EXTRAS_DEPTH:
        raise _validation_error("extras_invalid", "extras tiene demasiados niveles.")
    counter[0] += 1
    if counter[0] > _MAX_EXTRAS_ITEMS:
        raise _validation_error("extras_invalid", "extras tiene demasiados elementos.")

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _validation_error("extras_invalid", "extras contiene numeros no validos.")
        return 0.0 if value == 0 else value
    if isinstance(value, str):
        normalized_value = _nfc(value)
        if "\x00" in normalized_value:
            raise _validation_error("extras_invalid", "extras contiene caracteres no validos.")
        return normalized_value
    if isinstance(value, list):
        return [
            _normalize_extra_value(item, depth=depth + 1, counter=counter)
            for item in value
        ]
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise _validation_error("extras_invalid", "extras contiene una clave no valida.")
            key = _nfc(raw_key).strip()
            if not key or "\x00" in key or key in normalized:
                raise _validation_error("extras_invalid", "extras contiene una clave no valida.")
            if key.casefold() in _RESERVED_EXTRAS_KEYS:
                raise _validation_error(
                    "extras_reserved_key",
                    "extras contiene una clave reservada.",
                )
            normalized[key] = _normalize_extra_value(
                raw_value,
                depth=depth + 1,
                counter=counter,
            )
        return normalized
    raise _validation_error("extras_invalid", "extras contiene un valor no valido.")


def _normalize_extras(payload: dict[str, Any]) -> dict[str, Any]:
    has_extras = "extras" in payload
    has_metadata = "metadata" in payload
    extras_value = payload.get("extras") if has_extras else None
    metadata_value = payload.get("metadata") if has_metadata else None

    if has_extras and has_metadata and extras_value != metadata_value:
        raise _validation_error(
            "extras_ambiguous",
            "extras y metadata no pueden contener valores diferentes.",
        )
    raw = metadata_value if has_metadata else extras_value
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise _validation_error("extras_invalid", "extras debe ser un objeto JSON.")

    normalized = _normalize_extra_value(raw, depth=0, counter=[0])
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_EXTRAS_BYTES:
        raise _validation_error("extras_too_large", "extras supera el tamano permitido.")
    return normalized


def normalize_tenant_claim_payload(payload: Any) -> NormalizedTenantClaim:
    if not isinstance(payload, dict):
        raise _validation_error("claim_payload_invalid", "El reclamo debe ser un objeto JSON.")

    descripcion = _normalize_text(
        payload.get("descripcion"),
        field="descripcion",
        required=True,
        max_length=_MAX_DESCRIPTION_LENGTH,
    )
    categoria = _normalize_text(
        payload.get("categoria"),
        field="categoria",
        required=False,
        max_length=_MAX_CATEGORY_LENGTH,
    )
    latitud = _normalize_coordinate(payload.get("lat"), field="lat", lower=-90, upper=90)
    longitud = _normalize_coordinate(payload.get("lng"), field="lng", lower=-180, upper=180)
    extras = _normalize_extras(payload)

    canonical_object = {
        "categoria": categoria,
        "descripcion": descripcion,
        "extras": extras,
        "lat": latitud,
        "lng": longitud,
    }
    canonical_payload = json.dumps(
        canonical_object,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return NormalizedTenantClaim(
        descripcion=descripcion or "",
        categoria=categoria,
        latitud=latitud,
        longitud=longitud,
        extras=extras,
        canonical_payload=canonical_payload,
        payload_hash=hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest(),
    )


def tenant_claim_receipt_secret(version: str = TENANT_CLAIM_RECEIPT_SECRET_VERSION) -> bytes:
    config_name = TENANT_CLAIM_RECEIPT_SECRET_CONFIGS.get(str(version or ""))
    if not config_name:
        raise TenantClaimReceiptSecretUnavailable("unsupported_receipt_secret_version")
    configured = current_app.config.get(config_name)
    if isinstance(configured, bytes):
        secret = configured
    elif isinstance(configured, str):
        try:
            secret = configured.encode("utf-8")
        except UnicodeEncodeError:
            secret = b""
    else:
        secret = b""
    if len(secret) < _MIN_SECRET_BYTES:
        raise TenantClaimReceiptSecretUnavailable("receipt_secret_unavailable")
    return secret


def tenant_claim_idempotency_hash(*, tenant_id: int, key: str, secret: bytes) -> str:
    message = f"tenant-claim-idempotency\0v1\0{tenant_id}\0{key}".encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def derive_tenant_claim_pin(ticket: TenantTicket, *, secret: bytes | None = None) -> str:
    version = str(ticket.claim_receipt_secret_version or "")
    if secret is None:
        secret = tenant_claim_receipt_secret(version)
    elif version != TENANT_CLAIM_RECEIPT_SECRET_VERSION:
        raise TenantClaimReceiptSecretUnavailable("receipt_secret_version_mismatch")
    message = (
        f"tenant-claim-tracking-pin\0{version}\0{ticket.tenant_id}\0{ticket.id}"
    ).encode("utf-8")
    digest = hmac.new(secret, message, hashlib.sha256).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


def validate_tenant_claim_pin(ticket: TenantTicket, supplied_pin: Any) -> bool:
    if not ticket.claim_receipt_secret_version:
        return False
    candidate = str(supplied_pin or "").strip()
    if not re.fullmatch(r"\d{6}", candidate):
        return False
    expected = derive_tenant_claim_pin(ticket)
    return hmac.compare_digest(expected, candidate)


def tenant_claim_payload_matches(ticket: TenantTicket, payload_hash: str) -> bool:
    stored = str(ticket.intake_payload_hash or "")
    return bool(stored) and hmac.compare_digest(stored, payload_hash)


def build_tenant_claim_intake_receipt(
    ticket: TenantTicket,
    *,
    deduplicated: bool,
    request_id: str,
    secret: bytes | None = None,
) -> dict[str, Any]:
    pin = derive_tenant_claim_pin(ticket, secret=secret)
    code = f"T-{ticket.id}"
    tracking_path = f"/tracking/claim/{code}#pin={pin}"
    return {
        "contract_version": TENANT_CLAIM_INTAKE_RECEIPT_CONTRACT_VERSION,
        "ok": True,
        "persisted": True,
        "deduplicated": bool(deduplicated),
        "request_id": request_id,
        "claim": {
            "id": ticket.id,
            "code": code,
            "status": str(ticket.estado or "nuevo"),
            "category": ticket.categoria,
            "created_at": datetime_to_iso_utc(ticket.created_at),
        },
        "access": {
            "mode": "code_pin",
            "pin": pin,
        },
        "tracking": {
            "path": tracking_path,
            "experience_endpoint": (
                f"/api/public/tracking/experience?kind=claim&code={code}"
            ),
            "credential_transport": "x-tracking-pin-header",
            "requires_pin": True,
        },
        "actions": [
            {
                "id": "track_claim",
                "label": "Ver seguimiento",
                "href": tracking_path,
            }
        ],
    }
