import os
import logging
import re
import threading
import unicodedata
import uuid
from pathlib import PurePath
from botocore.exceptions import BotoCoreError, ClientError
from urllib.parse import quote, unquote, urlparse

from services.media_cache_policy import cache_control_for_key
from services.outbox_execution_budget import (
    outbox_execution_budget_active,
    outbox_io_timeout_seconds,
)

logger = logging.getLogger(__name__)


DEFAULT_R2_SIGNED_URL_TTL_SECONDS = 900
DEFAULT_R2_UPLOAD_URL_TTL_SECONDS = 600


class R2SourceObjectChangedError(RuntimeError):
    """Raised when a conditional promotion observes a different source ETag."""


class R2ObjectStorageUnavailableError(RuntimeError):
    """Raised when R2 cannot give a definitive answer for an object operation."""


_R2_DEFINITE_NOT_FOUND_CODES = {
    "404",
    "nosuchkey",
    "nosuchobject",
    "notfound",
}
_R2_COPY_PRECONDITION_CODES = {
    "412",
    "conditionalrequestconflict",
    "preconditionfailed",
}


def _client_error_code_and_status(exc):
    response = exc.response if isinstance(getattr(exc, "response", None), dict) else {}
    error = response.get("Error") if isinstance(response.get("Error"), dict) else {}
    response_metadata = (
        response.get("ResponseMetadata")
        if isinstance(response.get("ResponseMetadata"), dict)
        else {}
    )
    error_code = str(error.get("Code") or "").strip().lower()
    try:
        status_code = int(response_metadata.get("HTTPStatusCode") or 0)
    except (TypeError, ValueError):
        status_code = 0
    return error_code, status_code


SENSITIVE_R2_CONTEXTS = {
    "adjunto",
    "adjuntos",
    "attachment",
    "attachments",
    "claim",
    "claims",
    "conversation",
    "conversations",
    "message",
    "messages",
    "pedido",
    "pedidos",
    "reclamo",
    "reclamos",
    "ticket",
    "tickets",
    "upload",
    "uploads",
}

R2_CONTEXT_PREFIXES = {
    "audio": ("static", "audio_cache"),
    "audio_cache": ("static", "audio_cache"),
    "avatar": ("tenants", "avatars"),
    "avatars": ("tenants", "avatars"),
    "brand": ("tenants", "brand"),
    "branding": ("tenants", "brand"),
    "catalog": ("pymes", "catalogos"),
    "catalogo": ("pymes", "catalogos"),
    "catalogos": ("pymes", "catalogos"),
    "claim": ("municipios", "reclamos"),
    "claims": ("municipios", "reclamos"),
    "logo": ("tenants", "logos"),
    "logos": ("tenants", "logos"),
    "market": ("pymes", "catalogos"),
    "marketplace": ("pymes", "catalogos"),
    "pedido": ("pymes", "pedidos"),
    "pedidos": ("pymes", "pedidos"),
    "product": ("pymes", "catalogos"),
    "productos": ("pymes", "catalogos"),
    "reclamo": ("municipios", "reclamos"),
    "reclamos": ("municipios", "reclamos"),
    "survey": ("municipios", "encuestas"),
    "surveys": ("municipios", "encuestas"),
    "ticket": ("municipios", "tickets"),
    "tickets": ("municipios", "tickets"),
}


def _safe_segment(value, fallback="asset", max_length=80):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-.")
    if not text:
        text = fallback
    return text[:max_length].strip("-.") or fallback


def _safe_file_extension(filename):
    extension = PurePath(str(filename or "")).suffix.lower()
    if not extension or not re.fullmatch(r"\.[a-z0-9]{1,12}", extension):
        return ""
    return extension


def normalise_r2_object_key(key):
    normalized = "/".join(str(key or "").replace("\\", "/").strip("/").split("/"))
    if not normalized or normalized.startswith("../") or "/../" in f"/{normalized}/":
        return None
    return normalized


def _safe_filename(filename, sensitive=False):
    extension = _safe_file_extension(filename)
    if sensitive:
        return f"{uuid.uuid4().hex}{extension}"

    original_name = PurePath(str(filename or "asset")).name
    stem = original_name[: -len(extension)] if extension else original_name
    return f"{_safe_segment(stem, fallback='asset', max_length=96)}{extension}"


def _context_segments(context_type):
    raw_context = str(context_type or "general")
    if "/" in raw_context or "\\" in raw_context:
        cleaned = tuple(
            _safe_segment(segment, fallback="asset")
            for segment in raw_context.replace("\\", "/").split("/")
            if segment
        )
        return cleaned or ("general",)

    normalized_context = _safe_segment(raw_context, fallback="general")
    return R2_CONTEXT_PREFIXES.get(normalized_context, ("general", normalized_context))


def _is_sensitive_context(context_type):
    raw_context = str(context_type or "")
    normalized = _safe_segment(raw_context)
    segments = set(raw_context.replace("\\", "/").lower().split("/"))
    return normalized in SENSITIVE_R2_CONTEXTS or bool(segments & SENSITIVE_R2_CONTEXTS)


class R2Service:
    def __init__(self):
        self.endpoint_url = os.environ.get("R2_ENDPOINT_URL")
        self.access_key_id = os.environ.get("R2_ACCESS_KEY_ID")
        self.secret_access_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        self.bucket_name = os.environ.get("R2_BUCKET_NAME")
        self.public_base_url = os.environ.get("R2_PUBLIC_BASE_URL")
        self.region_name = os.environ.get("R2_REGION", "auto")

        self.client = None
        self._client_initialization_attempted = False
        self._client_lock = threading.Lock()

    def _create_client(self, *, timeout_seconds=None):
        """Import boto3 and build the client only for the first object operation."""

        import boto3
        from botocore.config import Config

        config_kwargs = {"signature_version": "s3v4"}
        if timeout_seconds is not None:
            config_kwargs.update(
                connect_timeout=float(timeout_seconds),
                read_timeout=float(timeout_seconds),
                retries={"total_max_attempts": 1, "mode": "standard"},
            )

        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            region_name=self.region_name,
            config=Config(**config_kwargs),
        )

    def _get_client(self):
        bounded_timeout = outbox_io_timeout_seconds()
        if bounded_timeout is not None:
            if not (
                self.endpoint_url
                and self.access_key_id
                and self.secret_access_key
                and self.bucket_name
            ):
                return None
            try:
                # Never reuse a warm process client whose botocore socket and
                # retry policy predates the cron execution budget.
                return self._create_client(timeout_seconds=bounded_timeout)
            except Exception as exc:
                logger.error(
                    "Failed to initialize bounded R2 client error_type=%s",
                    type(exc).__name__,
                )
                return None

        client = self.client
        if client is not None:
            return client
        if not (
            self.endpoint_url
            and self.access_key_id
            and self.secret_access_key
            and self.bucket_name
        ):
            return None

        with self._client_lock:
            if self.client is not None:
                return self.client
            if self._client_initialization_attempted:
                return None
            self._client_initialization_attempted = True
            try:
                self.client = self._create_client()
                logger.info("R2 client initialized successfully")
            except Exception as exc:
                logger.error(
                    "Failed to initialize R2 client error_type=%s",
                    type(exc).__name__,
                )
                self.client = None
            return self.client

    def generate_key(self, filename, tenant_slug, context_type="general"):
        """
        Build a Cloudflare R2 object key for tenant assets.

        Public commerce and branding assets keep readable sanitized names.
        Sensitive complaint, ticket and conversation files use opaque names so
        object URLs and logs do not expose citizen/customer data.
        """
        tenant_segment = _safe_segment(tenant_slug, fallback="default")
        context_segments = _context_segments(context_type)
        filename_segment = _safe_filename(
            filename,
            sensitive=_is_sensitive_context(context_type),
        )

        if context_segments == ("static", "audio_cache"):
            return "/".join((*context_segments, filename_segment))

        if len(context_segments) >= 2 and context_segments[0] in {"municipios", "pymes", "tenants"}:
            return "/".join((context_segments[0], tenant_segment, *context_segments[1:], filename_segment))

        return "/".join((*context_segments, tenant_segment, filename_segment))

    def public_url_for_key(self, key):
        if not self.public_base_url:
            return None
        safe_key = quote(str(key or ""))
        return f"{self.public_base_url}/{safe_key}"

    def object_key_from_url(self, url_or_key):
        if not url_or_key:
            return None

        raw_value = str(url_or_key).strip()
        if not raw_value:
            return None

        if raw_value.startswith("/"):
            return None

        if "://" not in raw_value and not raw_value.startswith("//"):
            return normalise_r2_object_key(raw_value)

        if not self.public_base_url:
            return None

        parsed_value = urlparse(raw_value)
        parsed_base = urlparse(self.public_base_url)
        if parsed_value.netloc.lower() != parsed_base.netloc.lower():
            return None

        base_path = parsed_base.path.strip("/")
        value_path = parsed_value.path.strip("/")
        if base_path:
            if value_path == base_path:
                return None
            if not value_path.startswith(f"{base_path}/"):
                return None
            value_path = value_path[len(base_path) + 1 :]

        return normalise_r2_object_key(unquote(value_path))

    def is_public_asset_key(self, key, content_type=None):
        return cache_control_for_key(key, content_type).startswith("public")

    @property
    def is_configured(self):
        """Return whether object operations can be executed safely."""

        return bool(self._get_client() and self.bucket_name)

    def generate_presigned_upload_url(
        self,
        key,
        content_type,
        content_length,
        expires_in=None,
    ):
        """Create a short-lived, MIME- and length-bound PUT URL."""

        safe_key = normalise_r2_object_key(key)
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if isinstance(content_length, bool):
            normalized_content_length = 0
        else:
            try:
                normalized_content_length = int(content_length)
            except (TypeError, ValueError):
                normalized_content_length = 0
        if (
            not self.is_configured
            or not safe_key
            or not normalized_content_type
            or normalized_content_length <= 0
        ):
            return None

        try:
            ttl = int(expires_in or DEFAULT_R2_UPLOAD_URL_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = DEFAULT_R2_UPLOAD_URL_TTL_SECONDS
        ttl = max(60, min(ttl, 900))

        try:
            return self._get_client().generate_presigned_url(
                "put_object",
                Params={
                    "Bucket": self.bucket_name,
                    "Key": safe_key,
                    "ContentType": normalized_content_type,
                    "ContentLength": normalized_content_length,
                },
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as exc:
            logger.error(
                "R2 upload URL generation failed error_type=%s",
                type(exc).__name__,
            )
            return None
        except Exception as exc:
            logger.error(
                "Unexpected R2 upload URL generation failure error_type=%s",
                type(exc).__name__,
            )
            return None

    def head_object(self, key):
        """Return metadata, ``None`` for definite absence, or a typed outage."""

        safe_key = normalise_r2_object_key(key)
        if not self.is_configured or not safe_key:
            raise R2ObjectStorageUnavailableError(
                "R2 HEAD is unavailable because storage is not configured"
            )
        try:
            return self._get_client().head_object(Bucket=self.bucket_name, Key=safe_key)
        except ClientError as exc:
            error_code, _ = _client_error_code_and_status(exc)
            if error_code in _R2_DEFINITE_NOT_FOUND_CODES:
                return None
            logger.error(
                "R2 HEAD unavailable error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 HEAD did not return a definitive result"
            ) from exc
        except BotoCoreError as exc:
            logger.error(
                "R2 HEAD unavailable error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 HEAD did not return a definitive result"
            ) from exc
        except Exception as exc:
            logger.error(
                "Unexpected R2 HEAD failure error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 HEAD did not return a definitive result"
            ) from exc

    def copy_object(
        self,
        source_key,
        destination_key,
        content_type,
        *,
        source_etag,
    ):
        """Promote a temporary object only if its trusted HEAD ETag still matches."""

        safe_source = normalise_r2_object_key(source_key)
        safe_destination = normalise_r2_object_key(destination_key)
        normalized_content_type = str(content_type or "").split(";", 1)[0].strip().lower()
        normalized_source_etag = str(source_etag or "").strip()
        if (
            not self.is_configured
            or not safe_source
            or not safe_destination
            or not normalized_content_type
            or not normalized_source_etag
        ):
            raise R2ObjectStorageUnavailableError(
                "R2 COPY is unavailable because its contract is incomplete"
            )

        try:
            self._get_client().copy_object(
                Bucket=self.bucket_name,
                Key=safe_destination,
                CopySource={"Bucket": self.bucket_name, "Key": safe_source},
                CopySourceIfMatch=normalized_source_etag,
                MetadataDirective="REPLACE",
                ContentType=normalized_content_type,
                CacheControl=cache_control_for_key(
                    safe_destination,
                    normalized_content_type,
                ),
            )
            return True
        except ClientError as exc:
            error_code, status_code = _client_error_code_and_status(exc)
            if status_code == 412 or error_code in _R2_COPY_PRECONDITION_CODES:
                raise R2SourceObjectChangedError(
                    "R2 source object changed before conditional copy"
                ) from exc
            logger.error(
                "R2 COPY unavailable error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 COPY did not return a definitive result"
            ) from exc
        except BotoCoreError as exc:
            logger.error(
                "R2 COPY unavailable error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 COPY did not return a definitive result"
            ) from exc
        except Exception as exc:
            logger.error(
                "Unexpected R2 copy failure error_type=%s",
                type(exc).__name__,
            )
            raise R2ObjectStorageUnavailableError(
                "R2 COPY did not return a definitive result"
            ) from exc

    def delete_object(self, key):
        """Best-effort deletion for temporary or superseded objects."""

        safe_key = normalise_r2_object_key(key)
        if not self.is_configured or not safe_key:
            return False
        try:
            self._get_client().delete_object(Bucket=self.bucket_name, Key=safe_key)
            return True
        except (BotoCoreError, ClientError) as exc:
            logger.warning(
                "R2 delete failed error_type=%s",
                type(exc).__name__,
            )
            return False
        except Exception as exc:
            logger.error(
                "Unexpected R2 delete failure error_type=%s",
                type(exc).__name__,
            )
            return False

    def generate_presigned_download_url(self, key, expires_in=None):
        client = self._get_client()
        if not client or not self.bucket_name or not key:
            return None

        try:
            ttl = int(expires_in or os.environ.get("R2_SIGNED_URL_TTL_SECONDS") or DEFAULT_R2_SIGNED_URL_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = DEFAULT_R2_SIGNED_URL_TTL_SECONDS
        ttl = max(60, min(ttl, 3600))

        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket_name, "Key": key},
                ExpiresIn=ttl,
            )
        except (BotoCoreError, ClientError) as e:
            logger.error(f"R2 signed URL generation failed for key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error generating R2 signed URL for key {key}: {e}")
            return None

    def resolve_download_url(self, key, content_type=None, expires_in=None):
        if self.is_public_asset_key(key, content_type):
            return self.public_url_for_key(key)
        return self.generate_presigned_download_url(key, expires_in=expires_in)

    def upload_file(self, file_obj, filename, content_type, tenant_slug, context_type="general"):
        """
        Uploads a file to Cloudflare R2.

        Args:
            file_obj: File-like object (bytes)
            filename: Original filename
            content_type: MIME type of the file
            tenant_slug: Slug of the tenant (for folder structure)
            context_type: Type of content (e.g., 'catalogos', 'reclamos', 'logos', 'eventos')

        Returns:
            str: Public URL of the uploaded file, or None if upload fails.
        """
        if not self.bucket_name:
            logger.warning("R2 is not configured or initialized.")
            return None

        key = self.generate_key(filename, tenant_slug, context_type=context_type)
        result = self.upload_file_with_key(file_obj, key, content_type)
        if result is None:
            logger.warning("R2 is not configured or the upload failed.")
        return result

    def upload_file_with_key(self, file_obj, key, content_type):
        """
        Uploads a file with a specific key.
        """
        client = self._get_client()
        if not client or not self.bucket_name:
            return None

        try:
            extra_args = {
                'ContentType': content_type,
                'CacheControl': cache_control_for_key(key, content_type),
            }

            upload_kwargs = {"ExtraArgs": extra_args}
            if outbox_execution_budget_active():
                from boto3.s3.transfer import TransferConfig

                # The cron accepts at most one provider request per upload;
                # multipart transfer would multiply the per-attempt timeout.
                upload_kwargs["Config"] = TransferConfig(
                    multipart_threshold=5 * 1024 * 1024 * 1024,
                    use_threads=False,
                )
            client.upload_fileobj(
                file_obj,
                self.bucket_name,
                key,
                **upload_kwargs,
            )

            # Existing callers expect the upload result to be the configured
            # public CDN URL. Private download flows should use
            # resolve_download_url/generate_presigned_download_url.
            return self.public_url_for_key(key)

        except (BotoCoreError, ClientError) as e:
            logger.error(f"R2 Upload Failed for key {key}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error uploading to R2: {e}")
            return None
        finally:
            if outbox_execution_budget_active():
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

# Singleton instance
r2_service = R2Service()
