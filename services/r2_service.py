import os
import logging
import re
import unicodedata
import uuid
from pathlib import PurePath
import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from urllib.parse import quote, unquote, urlparse

from services.media_cache_policy import cache_control_for_key

logger = logging.getLogger(__name__)


DEFAULT_R2_SIGNED_URL_TTL_SECONDS = 900

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
        if self.endpoint_url and self.access_key_id and self.secret_access_key:
            try:
                self.client = boto3.client(
                    's3',
                    endpoint_url=self.endpoint_url,
                    aws_access_key_id=self.access_key_id,
                    aws_secret_access_key=self.secret_access_key,
                    region_name=self.region_name,
                    config=Config(signature_version='s3v4')
                )
                logger.info("R2 Client initialized successfully")
            except Exception as e:
                logger.error(f"Failed to initialize R2 client: {e}")

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

    def generate_presigned_download_url(self, key, expires_in=None):
        if not self.client or not self.bucket_name or not key:
            return None

        try:
            ttl = int(expires_in or os.environ.get("R2_SIGNED_URL_TTL_SECONDS") or DEFAULT_R2_SIGNED_URL_TTL_SECONDS)
        except (TypeError, ValueError):
            ttl = DEFAULT_R2_SIGNED_URL_TTL_SECONDS
        ttl = max(60, min(ttl, 3600))

        try:
            return self.client.generate_presigned_url(
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
        if not self.client or not self.bucket_name:
            logger.warning("R2 is not configured or initialized.")
            return None

        key = self.generate_key(filename, tenant_slug, context_type=context_type)
        return self.upload_file_with_key(file_obj, key, content_type)

    def upload_file_with_key(self, file_obj, key, content_type):
        """
        Uploads a file with a specific key.
        """
        if not self.client or not self.bucket_name:
            return None

        try:
            extra_args = {
                'ContentType': content_type,
                'CacheControl': cache_control_for_key(key, content_type),
            }

            self.client.upload_fileobj(
                file_obj,
                self.bucket_name,
                key,
                ExtraArgs=extra_args
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

# Singleton instance
r2_service = R2Service()
