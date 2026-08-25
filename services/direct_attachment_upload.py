from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any, NoReturn

from flask import current_app, g, request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from limits import parse
from sqlalchemy import text
from werkzeug.utils import secure_filename

from extensions import db, limiter
from models import ArchivoAdjunto, TenantProfile, User
from services.attachment_delivery import serialize_attachment_for_delivery
from services.r2_service import (
    R2ObjectStorageUnavailableError,
    R2Service,
    R2SourceObjectChangedError,
    r2_service,
)
from utils.auth_helpers import auth_tenant_for_user
from utils.roles import normalize_tenant_slug


DIRECT_UPLOAD_CONTRACT_VERSION = "chat.attachment.direct.v1"
DIRECT_UPLOAD_ERROR_CONTRACT_VERSION = "chat.attachment.direct.error.v1"
DIRECT_UPLOAD_TOKEN_SALT = "chat-attachment-direct-upload-v1"
DIRECT_UPLOAD_TTL_SECONDS = 600
MAX_DIRECT_UPLOAD_TTL_SECONDS = 900
MAX_SESSION_ID_LENGTH = 36
DEFAULT_DIRECT_UPLOAD_PREPARE_ACTOR_RATE_LIMIT = "20 per 60 seconds"
DEFAULT_DIRECT_UPLOAD_PREPARE_GLOBAL_RATE_LIMIT = "500 per 60 seconds"
_DIRECT_UPLOAD_PREPARE_RATE_LIMIT_NAMESPACE = "chat-attachment-direct-prepare-v1"
_MAX_CONFIGURED_RATE_LIMIT_AMOUNT = 100_000
_MAX_CONFIGURED_RATE_LIMIT_WINDOW_SECONDS = 86_400
OBJECT_STORAGE_RETRY_AFTER_SECONDS = 5


class DirectAttachmentUploadError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        retryable: bool | None = None,
        retry_after_seconds: int = 0,
        rate_limit: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = int(status_code)
        self.retryable = self.status_code >= 500 if retryable is None else bool(retryable)
        self.retry_after_seconds = max(0, int(retry_after_seconds))
        self.rate_limit = dict(rate_limit or {})


@dataclass(frozen=True)
class DirectUploadScope:
    tenant_slug: str
    owner_user_id: int
    attachment_user_id: int
    actor_kind: str
    actor_fingerprint: str
    session_id: str
    session_fingerprint: str


def _configured_rate_limit(config_key: str, default: str):
    raw = current_app.config.get(config_key)
    if raw is None:
        raw = os.getenv(config_key, default)
    try:
        item = parse(str(raw).strip())
        amount = int(item.amount)
        window_seconds = int(item.get_expiry())
    except Exception as exc:
        raise DirectAttachmentUploadError(
            "direct_upload_rate_limit_configuration_invalid",
            "La politica de capacidad de carga directa no esta disponible.",
            503,
            retryable=False,
        ) from exc
    if (
        amount <= 0
        or amount > _MAX_CONFIGURED_RATE_LIMIT_AMOUNT
        or window_seconds <= 0
        or window_seconds > _MAX_CONFIGURED_RATE_LIMIT_WINDOW_SECONDS
    ):
        raise DirectAttachmentUploadError(
            "direct_upload_rate_limit_configuration_invalid",
            "La politica de capacidad de carga directa no esta disponible.",
            503,
            retryable=False,
        )
    return item, amount, window_seconds


def _deployment_requires_shared_rate_limit_storage() -> bool:
    environment = str(current_app.config.get("ENV") or "").strip().lower()
    return bool(
        os.getenv("VERCEL")
        or os.getenv("VERCEL_ENV")
        or environment in {"prod", "production"}
    )


def _assert_shared_rate_limit_storage() -> None:
    if not _deployment_requires_shared_rate_limit_storage():
        return
    storage_uri = str(
        current_app.config.get("RATELIMIT_STORAGE_URI")
        or os.getenv("RATELIMIT_STORAGE_URI")
        or ""
    ).strip().lower()
    if storage_uri and not storage_uri.startswith("memory://"):
        return
    raise DirectAttachmentUploadError(
        "direct_upload_rate_limit_unavailable",
        "El control compartido de capacidad no esta disponible temporalmente.",
        503,
        retryable=True,
        retry_after_seconds=60,
        rate_limit={
            "scope": "shared_storage",
            "available": False,
            "retry_after_seconds": 60,
        },
    )


def _rate_scope_key(scope: DirectUploadScope) -> str:
    material = (
        f"{scope.tenant_slug}\x1f{scope.actor_kind}\x1f{scope.actor_fingerprint}"
    ).encode("utf-8", "strict")
    return hashlib.sha256(material).hexdigest()


def _consume_prepare_rate_limit(
    *,
    bucket_scope: str,
    bucket_key: str,
    config_key: str,
    default: str,
) -> dict[str, Any]:
    item, amount, window_seconds = _configured_rate_limit(config_key, default)
    identifiers = (
        _DIRECT_UPLOAD_PREPARE_RATE_LIMIT_NAMESPACE,
        bucket_scope,
        bucket_key,
    )
    try:
        strategy = limiter.limiter
        allowed = bool(strategy.hit(item, *identifiers))
        window = strategy.get_window_stats(item, *identifiers)
        remaining = max(0, int(window.remaining))
        reset_after = max(
            1,
            int(math.ceil(float(window.reset_time) - time.time())),
        )
    except Exception as exc:
        current_app.logger.exception(
            "[direct-upload] Shared prepare rate limiter unavailable; request denied"
        )
        metadata = {
            "scope": bucket_scope,
            "available": False,
            "limit": amount,
            "remaining": 0,
            "window_seconds": window_seconds,
            "retry_after_seconds": window_seconds,
        }
        raise DirectAttachmentUploadError(
            "direct_upload_rate_limit_unavailable",
            "El control compartido de capacidad no esta disponible temporalmente.",
            503,
            retryable=True,
            retry_after_seconds=window_seconds,
            rate_limit=metadata,
        ) from exc

    metadata = {
        "scope": bucket_scope,
        "available": True,
        "limit": amount,
        "remaining": remaining,
        "window_seconds": window_seconds,
        "retry_after_seconds": 0 if allowed else reset_after,
    }
    if not allowed:
        raise DirectAttachmentUploadError(
            "direct_upload_rate_limited",
            "Se alcanzo el limite temporal de preparacion de adjuntos.",
            429,
            retryable=True,
            retry_after_seconds=reset_after,
            rate_limit=metadata,
        )
    return metadata


def enforce_prepare_rate_limits(scope: DirectUploadScope) -> tuple[dict[str, Any], ...]:
    """Consume shared tenant/actor and global prepare capacity, or fail closed."""

    _assert_shared_rate_limit_storage()
    actor_decision = _consume_prepare_rate_limit(
        bucket_scope="tenant_actor",
        bucket_key=_rate_scope_key(scope),
        config_key="DIRECT_UPLOAD_PREPARE_ACTOR_RATE_LIMIT",
        default=DEFAULT_DIRECT_UPLOAD_PREPARE_ACTOR_RATE_LIMIT,
    )
    global_decision = _consume_prepare_rate_limit(
        bucket_scope="global",
        bucket_key="all",
        config_key="DIRECT_UPLOAD_PREPARE_GLOBAL_RATE_LIMIT",
        default=DEFAULT_DIRECT_UPLOAD_PREPARE_GLOBAL_RATE_LIMIT,
    )
    return actor_decision, global_decision


def _signing_secret() -> bytes:
    secret = os.getenv("SECRET_KEY") or current_app.config.get("SECRET_KEY")
    if not secret:
        raise DirectAttachmentUploadError(
            "upload_signing_unavailable",
            "La carga directa no esta disponible temporalmente.",
            503,
        )
    if (os.getenv("VERCEL") or os.getenv("VERCEL_ENV")) and not os.getenv("SECRET_KEY"):
        raise DirectAttachmentUploadError(
            "upload_signing_unavailable",
            "La carga directa no esta disponible temporalmente.",
            503,
        )
    return str(secret).encode("utf-8")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        secret_key=_signing_secret(),
        salt=DIRECT_UPLOAD_TOKEN_SALT,
        signer_kwargs={"digest_method": hashlib.sha256},
    )


def _fingerprint(label: str, value: object) -> str:
    return hmac.new(
        _signing_secret(),
        f"{label}:{value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _normalized_session_id() -> str:
    session_id = str(request.headers.get("X-Chat-Session-Id") or "").strip()
    if not session_id:
        raise DirectAttachmentUploadError(
            "chat_session_required",
            "X-Chat-Session-Id es requerido para la carga directa.",
            400,
        )
    if len(session_id) > MAX_SESSION_ID_LENGTH or any(ord(char) < 32 for char in session_id):
        raise DirectAttachmentUploadError(
            "invalid_chat_session",
            "X-Chat-Session-Id no es valido.",
            400,
        )
    return session_id


def _authoritative_tenant(owner_user: User | None) -> TenantProfile:
    if owner_user is None or getattr(owner_user, "id", None) is None:
        raise DirectAttachmentUploadError(
            "tenant_scope_required",
            "La carga directa requiere un contexto de entidad valido.",
            403,
        )

    tenant = auth_tenant_for_user(owner_user)
    if tenant is None:
        raise DirectAttachmentUploadError(
            "tenant_scope_required",
            "No se pudo resolver un tenant unico para la carga.",
            403,
        )

    tenant_slug = normalize_tenant_slug(getattr(tenant, "slug", None))
    if not tenant_slug:
        raise DirectAttachmentUploadError(
            "tenant_scope_required",
            "El tenant no tiene un identificador valido.",
            403,
        )

    explicit_slug = normalize_tenant_slug(
        request.headers.get("X-Tenant-Slug") or request.headers.get("X-Tenant")
    )
    if explicit_slug and explicit_slug != tenant_slug:
        raise DirectAttachmentUploadError(
            "tenant_scope_mismatch",
            "El tenant solicitado no coincide con la identidad autenticada.",
            403,
        )
    return tenant


def resolve_direct_upload_scope(
    *,
    current_user: User | None,
    owner_user: User | None,
    anon_id: str | None,
) -> DirectUploadScope:
    tenant = _authoritative_tenant(owner_user)
    session_id = _normalized_session_id()
    jwt_user = getattr(g, "jwt_user", None)
    principal = current_user or jwt_user

    if principal is not None and getattr(principal, "id", None) is not None:
        actor_kind = "user"
        actor_value = int(principal.id)
    else:
        normalized_anon_id = str(anon_id or "").strip()
        if not normalized_anon_id:
            raise DirectAttachmentUploadError(
                "anonymous_identity_required",
                "No se pudo validar la identidad anonima de la carga.",
                403,
            )
        actor_kind = "anonymous"
        actor_value = normalized_anon_id

    owner_id = int(owner_user.id)
    return DirectUploadScope(
        tenant_slug=normalize_tenant_slug(tenant.slug),
        owner_user_id=owner_id,
        attachment_user_id=owner_id,
        actor_kind=actor_kind,
        actor_fingerprint=_fingerprint(actor_kind, actor_value),
        session_id=session_id,
        session_fingerprint=_fingerprint("session", session_id),
    )


def _normalize_mime_type(raw_mime_type: object) -> str:
    return str(raw_mime_type or "").split(";", 1)[0].strip().lower()


def _safe_original_filename(raw_filename: object) -> str:
    filename = secure_filename(str(raw_filename or "").strip())
    if not filename:
        raise DirectAttachmentUploadError(
            "invalid_filename",
            "El nombre del archivo no es valido.",
            400,
        )
    return filename[:255]


def _size_bytes(raw_size: object, max_file_bytes: int) -> int:
    if isinstance(raw_size, bool):
        size = 0
    else:
        try:
            size = int(raw_size)
        except (TypeError, ValueError):
            size = 0
    if size <= 0:
        raise DirectAttachmentUploadError(
            "invalid_file_size",
            "size_bytes debe ser un entero positivo.",
            400,
        )
    if size > int(max_file_bytes):
        raise DirectAttachmentUploadError(
            "file_too_large",
            "El archivo supera el limite permitido.",
            413,
        )
    return size


def _token_claims(token: object) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise DirectAttachmentUploadError(
            "upload_intent_required",
            "intent_token es requerido.",
            400,
        )
    try:
        payload = _serializer().loads(
            token.strip(),
            max_age=MAX_DIRECT_UPLOAD_TTL_SECONDS,
        )
    except SignatureExpired as exc:
        raise DirectAttachmentUploadError(
            "upload_intent_expired",
            "La intencion de carga vencio. Inicia una nueva carga.",
            410,
        ) from exc
    except BadSignature as exc:
        raise DirectAttachmentUploadError(
            "invalid_upload_intent",
            "La intencion de carga no es valida.",
            400,
        ) from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise DirectAttachmentUploadError(
            "invalid_upload_intent",
            "La intencion de carga no es valida.",
            400,
        )
    if int(payload.get("expires_at") or 0) < int(time.time()):
        raise DirectAttachmentUploadError(
            "upload_intent_expired",
            "La intencion de carga vencio. Inicia una nueva carga.",
            410,
        )
    return payload


def _assert_scope_matches(scope: DirectUploadScope, claims: dict[str, Any]) -> None:
    expected = {
        "tenant_slug": scope.tenant_slug,
        "owner_user_id": scope.owner_user_id,
        "attachment_user_id": scope.attachment_user_id,
        "actor_kind": scope.actor_kind,
        "actor_fingerprint": scope.actor_fingerprint,
        "session_fingerprint": scope.session_fingerprint,
    }
    if any(not hmac.compare_digest(str(claims.get(key)), str(value)) for key, value in expected.items()):
        raise DirectAttachmentUploadError(
            "upload_scope_mismatch",
            "La intencion de carga no pertenece a esta sesion o tenant.",
            409,
        )


def prepare_direct_attachment_upload(
    payload: dict[str, Any],
    *,
    scope: DirectUploadScope,
    allowed_mime_types: set[str],
    max_file_bytes: int,
    storage: R2Service | None = None,
) -> dict[str, Any]:
    storage = storage or r2_service
    if not storage.is_configured:
        raise DirectAttachmentUploadError(
            "object_storage_unavailable",
            "El almacenamiento de adjuntos no esta disponible temporalmente.",
            503,
        )

    filename = _safe_original_filename(payload.get("filename"))
    mime_type = _normalize_mime_type(
        payload.get("mime_type") or payload.get("mimeType")
    )
    if mime_type not in allowed_mime_types:
        raise DirectAttachmentUploadError(
            "unsupported_mime_type",
            "El tipo de archivo no esta permitido.",
            400,
        )
    size_bytes = _size_bytes(
        payload.get("size_bytes", payload.get("size")),
        max_file_bytes,
    )
    enforce_prepare_rate_limits(scope)

    upload_id = uuid.uuid4().hex
    extension = PurePath(filename).suffix.lower()
    temporary_key = f"uploads/{scope.tenant_slug}/chat-attachments/{upload_id}{extension}"
    final_key = storage.generate_key(
        filename,
        scope.tenant_slug,
        context_type="attachments",
    )
    expires_in_seconds = DIRECT_UPLOAD_TTL_SECONDS
    expires_at = int(time.time()) + expires_in_seconds
    upload_url = storage.generate_presigned_upload_url(
        temporary_key,
        mime_type,
        size_bytes,
        expires_in=expires_in_seconds,
    )
    if not upload_url:
        raise DirectAttachmentUploadError(
            "upload_url_unavailable",
            "No se pudo preparar la carga directa.",
            503,
        )

    claims = {
        "v": 1,
        "upload_id": upload_id,
        "temporary_key": temporary_key,
        "final_key": final_key,
        "filename": filename,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "tenant_slug": scope.tenant_slug,
        "owner_user_id": scope.owner_user_id,
        "attachment_user_id": scope.attachment_user_id,
        "actor_kind": scope.actor_kind,
        "actor_fingerprint": scope.actor_fingerprint,
        "session_fingerprint": scope.session_fingerprint,
        "expires_at": expires_at,
    }
    return {
        "ok": True,
        "contract_version": DIRECT_UPLOAD_CONTRACT_VERSION,
        "operation": "prepare_direct_upload",
        "upload_id": upload_id,
        "intent_token": _serializer().dumps(claims),
        "upload": {
            "method": "PUT",
            "url": upload_url,
            "headers": {"Content-Type": mime_type},
            "expires_in_seconds": expires_in_seconds,
        },
        "constraints": {
            "max_file_bytes": int(max_file_bytes),
            "exact_size_required": True,
            "allowed_mime_types": sorted(allowed_mime_types),
        },
    }


def _existing_attachment(
    *,
    claims: dict[str, Any],
    scope: DirectUploadScope,
) -> ArchivoAdjunto | None:
    return ArchivoAdjunto.query.filter_by(
        url=claims["final_key"],
        tipo="chat_adjunto",
        user_id=scope.attachment_user_id,
        session_id=scope.session_id,
    ).first()


def _acquire_completion_lock(upload_id: object) -> None:
    """Serialize completion for one intent across Vercel instances on Postgres."""

    try:
        bind = db.session.get_bind()
        if bind.dialect.name != "postgresql":
            return
        lock_key = int.from_bytes(
            hashlib.sha256(str(upload_id).encode("utf-8")).digest()[:8],
            byteorder="big",
            signed=True,
        )
        db.session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": lock_key},
        )
    except Exception as exc:
        db.session.rollback()
        raise DirectAttachmentUploadError(
            "completion_lock_unavailable",
            "No se pudo asegurar la finalizacion idempotente del adjunto.",
            503,
        ) from exc


def _cleanup_upload_objects(storage: R2Service, *keys: object) -> None:
    """Best-effort cleanup without allowing a delete failure to mask the contract."""

    for key in dict.fromkeys(str(value or "").strip() for value in keys):
        if not key:
            continue
        try:
            storage.delete_object(key)
        except Exception:
            current_app.logger.exception(
                "[direct-upload] Object cleanup failed during rejected completion"
            )


def _verified_object_metadata(
    metadata: object,
    *,
    expected_size: int,
    expected_mime: str,
) -> bool:
    if not isinstance(metadata, dict):
        return False
    try:
        actual_size = int(metadata.get("ContentLength"))
    except (TypeError, ValueError):
        actual_size = -1
    actual_mime = _normalize_mime_type(metadata.get("ContentType"))
    return actual_size == int(expected_size) and actual_mime == expected_mime


def _raise_object_storage_unavailable(exc: Exception) -> NoReturn:
    db.session.rollback()
    raise DirectAttachmentUploadError(
        "object_storage_temporarily_unavailable",
        "El almacenamiento de adjuntos no respondio de forma concluyente.",
        503,
        retryable=True,
        retry_after_seconds=OBJECT_STORAGE_RETRY_AFTER_SECONDS,
    ) from exc


def complete_direct_attachment_upload(
    payload: dict[str, Any],
    *,
    scope: DirectUploadScope,
    storage: R2Service | None = None,
) -> dict[str, Any]:
    storage = storage or r2_service
    if not storage.is_configured:
        raise DirectAttachmentUploadError(
            "object_storage_unavailable",
            "El almacenamiento de adjuntos no esta disponible temporalmente.",
            503,
        )

    claims = _token_claims(payload.get("intent_token"))
    _assert_scope_matches(scope, claims)
    _acquire_completion_lock(claims.get("upload_id"))
    existing = _existing_attachment(claims=claims, scope=scope)
    if existing is not None:
        db.session.commit()
        storage.delete_object(claims["temporary_key"])
        return {
            "ok": True,
            "contract_version": DIRECT_UPLOAD_CONTRACT_VERSION,
            "operation": "complete_direct_upload",
            "idempotent": True,
            "attachmentInfo": serialize_attachment_for_delivery(existing),
        }

    try:
        metadata = storage.head_object(claims["temporary_key"])
    except R2ObjectStorageUnavailableError as exc:
        _raise_object_storage_unavailable(exc)
    if not isinstance(metadata, dict):
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "uploaded_object_not_found",
            "Todavia no se encontro el archivo cargado en R2.",
            409,
        )
    if not _verified_object_metadata(
        metadata,
        expected_size=int(claims["size_bytes"]),
        expected_mime=claims["mime_type"],
    ):
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "uploaded_object_mismatch",
            "El archivo recibido no coincide con el tamaño o tipo declarado.",
            409,
        )

    source_etag = str(metadata.get("ETag") or "").strip()
    if not source_etag:
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "uploaded_object_metadata_invalid",
            "El archivo recibido no tiene metadata verificable.",
            409,
        )

    try:
        promoted = storage.copy_object(
            claims["temporary_key"],
            claims["final_key"],
            claims["mime_type"],
            source_etag=source_etag,
        )
    except R2SourceObjectChangedError as exc:
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "uploaded_object_changed",
            "El archivo cambio durante la finalizacion. Inicia una nueva carga.",
            409,
        ) from exc
    except R2ObjectStorageUnavailableError as exc:
        _raise_object_storage_unavailable(exc)
    if not promoted:
        _raise_object_storage_unavailable(
            R2ObjectStorageUnavailableError(
                "R2 COPY returned an ambiguous result"
            )
        )

    try:
        final_metadata = storage.head_object(claims["final_key"])
    except R2ObjectStorageUnavailableError as exc:
        _raise_object_storage_unavailable(exc)
    if not _verified_object_metadata(
        final_metadata,
        expected_size=int(claims["size_bytes"]),
        expected_mime=claims["mime_type"],
    ):
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "promoted_object_mismatch",
            "El archivo final no coincide con la carga verificada.",
            409,
        )

    attachment = ArchivoAdjunto(
        user_id=scope.attachment_user_id,
        session_id=scope.session_id,
        filename=PurePath(claims["final_key"]).name,
        nombre_original=claims["filename"],
        mime=claims["mime_type"],
        tamano=int(claims["size_bytes"]),
        tipo="chat_adjunto",
        url=claims["final_key"],
    )
    try:
        db.session.add(attachment)
        db.session.commit()
    except Exception:
        db.session.rollback()
        _cleanup_upload_objects(
            storage,
            claims["final_key"],
            claims["temporary_key"],
        )
        raise DirectAttachmentUploadError(
            "attachment_persistence_failed",
            "No se pudo registrar el adjunto.",
            503,
        )

    storage.delete_object(claims["temporary_key"])
    return {
        "ok": True,
        "contract_version": DIRECT_UPLOAD_CONTRACT_VERSION,
        "operation": "complete_direct_upload",
        "idempotent": False,
        "attachmentInfo": serialize_attachment_for_delivery(attachment),
    }
