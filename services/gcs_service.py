import logging
import os
import uuid
import io
import re
import shutil
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse, urljoin

import requests
from flask import current_app, has_app_context, has_request_context, request, g
from werkzeug.utils import secure_filename
from services.thumbnail_service import generar_thumbnail
from services.r2_service import r2_service

logger = logging.getLogger(__name__)


def _sanitize_path_segment(segment: str | None) -> str | None:
    """Return a filesystem-friendly representation of ``segment``."""

    if segment is None:
        return None

    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(segment).strip())
    sanitized = sanitized.strip("-_.")
    return sanitized or None


def _entity_subdir_from_owner(owner_user) -> str | None:
    """Infer a descriptive folder name for the entity handling the request."""

    if not owner_user:
        return None

    tipo_chat = getattr(owner_user, "tipo_chat", None)
    prefix = None
    if tipo_chat == "municipio":
        prefix = "municipio"
    elif tipo_chat == "pyme":
        prefix = "empresa"

    for attr in ("municipio_id", "pyme_id", "empresa_id", "id"):
        value = getattr(owner_user, attr, None)
        if value is None or value == "":
            continue
        identifier = _sanitize_path_segment(value)
        if not identifier:
            continue
        if not prefix:
            if attr == "municipio_id":
                prefix = "municipio"
            elif attr in {"pyme_id", "empresa_id"}:
                prefix = "empresa"
            else:
                prefix = "usuario"
        return f"{prefix}_{identifier}"

    if prefix and hasattr(owner_user, "id"):
        identifier = _sanitize_path_segment(getattr(owner_user, "id", None))
        if identifier:
            return f"{prefix}_{identifier}"

    return None


def _determine_fallback_subdir(explicit: str | None = None) -> str | None:
    """Determine which directory should store local fallback uploads."""

    candidate = _sanitize_path_segment(explicit)
    if candidate:
        return candidate

    if has_request_context():
        header_override = request.headers.get("X-Upload-Entity")
        header_candidate = _sanitize_path_segment(header_override)
        if header_candidate:
            return header_candidate

        owner = getattr(g, "owner_user", None)
        entity_subdir = _entity_subdir_from_owner(owner)
        if entity_subdir:
            return entity_subdir

    return None


def _resolve_local_upload_base() -> str:
    """Return the directory used to persist local uploads, preferring /data."""

    default_serving_dir = os.path.join(current_app.root_path, "static", "uploads")
    configured = current_app.config.get("LOCAL_UPLOAD_FOLDER") or os.environ.get("LOCAL_UPLOAD_FOLDER")

    preferred_physical = None
    if configured:
        preferred_physical = os.path.abspath(configured)
    else:
        persistent_root = os.environ.get("DATA_DIR") or "/data"
        candidate = os.path.join(os.path.abspath(persistent_root), "uploads")
        try:
            os.makedirs(candidate, exist_ok=True)
            preferred_physical = candidate
        except OSError:
            preferred_physical = None

    if not preferred_physical:
        return default_serving_dir

    try:
        os.makedirs(preferred_physical, exist_ok=True)
    except OSError:
        preferred_physical = default_serving_dir

    if os.path.abspath(preferred_physical) == os.path.abspath(default_serving_dir):
        return default_serving_dir

    try:
        os.makedirs(os.path.dirname(default_serving_dir), exist_ok=True)
    except OSError:
        return preferred_physical

    try:
        if os.path.islink(default_serving_dir):
            current_target = os.readlink(default_serving_dir)
            if os.path.abspath(current_target) != os.path.abspath(preferred_physical):
                os.unlink(default_serving_dir)
        elif os.path.exists(default_serving_dir):
            if os.path.isdir(default_serving_dir):
                existing_entries = os.listdir(default_serving_dir)
                if not existing_entries:
                    os.rmdir(default_serving_dir)
                else:
                    migrated_any = False
                    try:
                        for entry in existing_entries:
                            src_path = os.path.join(default_serving_dir, entry)
                            dest_path = os.path.join(preferred_physical, entry)
                            if os.path.exists(dest_path):
                                base, ext = os.path.splitext(entry)
                                suffix = 1
                                candidate = (
                                    f"{base}_{suffix}{ext}" if base else f"{entry}_{suffix}"
                                )
                                while os.path.exists(os.path.join(preferred_physical, candidate)):
                                    suffix += 1
                                    candidate = (
                                        f"{base}_{suffix}{ext}" if base else f"{entry}_{suffix}"
                                    )
                                dest_path = os.path.join(preferred_physical, candidate)
                            shutil.move(src_path, dest_path)
                            migrated_any = True
                    except Exception:
                        logger.warning(
                            "Failed to migrate existing uploads from '%s' to '%s'.",
                            default_serving_dir,
                            preferred_physical,
                            exc_info=True,
                        )
                        return default_serving_dir
                    try:
                        os.rmdir(default_serving_dir)
                    except OSError:
                        pass
                    if migrated_any:
                        logger.info(
                            "Migrated existing static uploads directory '%s' into persistent path '%s'.",
                            default_serving_dir,
                            preferred_physical,
                        )
            else:
                return default_serving_dir
        os.symlink(preferred_physical, default_serving_dir)
        return default_serving_dir
    except OSError:
        try:
            os.makedirs(default_serving_dir, exist_ok=True)
        except OSError:
            pass
        return default_serving_dir


def _get_request_base_url() -> str | None:
    """Return the preferred absolute base URL for the current request."""

    if not has_request_context():
        return None

    headers = request.headers
    forwarded_proto = headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
    forwarded_host = headers.get("X-Forwarded-Host", "").split(",")[0].strip()
    forwarded_port = headers.get("X-Forwarded-Port", "").split(",")[0].strip()

    scheme = forwarded_proto or getattr(request, "scheme", None) or "https"

    host = (
        forwarded_host
        or headers.get("Host")
        or getattr(request, "host", None)
        or ""
    ).split(",")[0].strip()

    if not host:
        return None

    if forwarded_port:
        normalized_port = forwarded_port
        if scheme.lower() == "https" and normalized_port == "443":
            normalized_port = ""
        elif scheme.lower() == "http" and normalized_port == "80":
            normalized_port = ""
        if normalized_port and ":" not in host:
            host = f"{host}:{normalized_port}"

    return f"{scheme}://{host}".rstrip("/")


_PRIVATE_IPV4_PREFIXES = ("10.", "127.", "169.254.", "192.168.")


def _is_private_host(host: str | None) -> bool:
    """Return ``True`` when ``host`` refers to a loopback or private address."""

    if not host:
        return True

    normalized = host.strip().lower()
    if not normalized:
        return True

    if normalized in {"localhost", "0.0.0.0"}:
        return True
    if normalized.startswith("::1") or normalized == "[::1]":
        return True

    for prefix in _PRIVATE_IPV4_PREFIXES:
        if normalized.startswith(prefix):
            return True

    if normalized.startswith("172."):
        try:
            second_octet = int(normalized.split(".")[1])
        except (IndexError, ValueError):
            pass
        else:
            if 16 <= second_octet <= 31:
                return True

    # Unique local IPv6 ranges (fc00::/7) and link-local (fe80::/10)
    if normalized.startswith("fc") or normalized.startswith("fd") or normalized.startswith("fe80"):
        return True

    return False


def _normalize_base_url(url: str | None) -> tuple[str | None, str | None]:
    """Return a normalized base URL and host tuple from ``url``."""

    if not url:
        return None, None

    candidate = url.strip()
    if not candidate:
        return None, None

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    netloc = parsed.netloc or parsed.path
    if not netloc:
        return None, None

    netloc = netloc.split("/", 1)[0]
    scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
    normalized_url = f"{scheme}://{netloc}".rstrip("/")
    host = parsed.hostname or netloc.split(":")[0]

    return normalized_url, host


def _purge_old_files(upload_dir: str, retention_days: int) -> None:
    """Delete files older than ``retention_days`` inside ``upload_dir``."""

    if retention_days <= 0:
        return

    cutoff = datetime.utcnow() - timedelta(days=retention_days)
    try:
        for entry in os.scandir(upload_dir):
            if not entry.is_file():
                continue
            entry_mtime = datetime.utcfromtimestamp(entry.stat().st_mtime)
            if entry_mtime < cutoff:
                try:
                    os.remove(entry.path)
                except OSError:
                    logger.warning(
                        "Failed to remove expired local upload '%s'", entry.path, exc_info=True
                    )
    except FileNotFoundError:
        return
    except Exception:  # pragma: no cover - best-effort clean-up
        logger.warning(
            "Unable to purge old files in '%s'", upload_dir, exc_info=True
        )


def _mask_sensitive_value(value: str) -> str:
    """Return a masked representation of a sensitive token for logging."""

    if not value:
        return ""

    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)

    prefix = text[:2]
    suffix = text[-2:]
    return f"{prefix}{'*' * (len(text) - 4)}{suffix}"


def _clean_env_value(name: str) -> str | None:
    """Return a trimmed config value from env or Flask config when available."""

    raw_value: Any = os.environ.get(name)

    if raw_value is None and has_app_context():
        raw_value = current_app.config.get(name)

    if raw_value is None:
        return None

    cleaned = str(raw_value).strip()
    return cleaned or None


def _init_cloudinary():  # pragma: no cover - thin wrapper validated via tests
    """Return a tuple ``(enabled, uploader, extra_options)`` based on env vars."""

    cloudinary_url = _clean_env_value("CLOUDINARY_URL")
    cloud_name = _clean_env_value("CLOUDINARY_CLOUD_NAME")
    api_key = _clean_env_value("CLOUDINARY_API_KEY")
    api_secret = _clean_env_value("CLOUDINARY_API_SECRET")
    upload_folder = _clean_env_value("CLOUDINARY_UPLOAD_FOLDER")

    config_kwargs: dict[str, str | bool] = {"secure": True}
    explicit_values = [cloud_name, api_key, api_secret]
    has_any_explicit = any(explicit_values)
    has_all_explicit = all(explicit_values)

    if has_all_explicit:
        config_kwargs.update(
            cloud_name=cloud_name, api_key=api_key, api_secret=api_secret
        )
        if cloudinary_url:
            logger.info(
                "CLOUDINARY_URL detected but overridden by explicit CLOUDINARY_* credentials."
            )
    elif cloudinary_url:
        config_kwargs["cloudinary_url"] = cloudinary_url
        if has_any_explicit and not has_all_explicit:
            missing = []
            if not cloud_name:
                missing.append("CLOUDINARY_CLOUD_NAME")
            if not api_key:
                missing.append("CLOUDINARY_API_KEY")
            if not api_secret:
                missing.append("CLOUDINARY_API_SECRET")
            logger.info(
                "Ignoring partial explicit Cloudinary credentials in favour of CLOUDINARY_URL (missing: %s)",
                ", ".join(missing),
            )
    elif has_any_explicit:
        missing = []
        if not cloud_name:
            missing.append("CLOUDINARY_CLOUD_NAME")
        if not api_key:
            missing.append("CLOUDINARY_API_KEY")
        if not api_secret:
            missing.append("CLOUDINARY_API_SECRET")
        logger.warning(
            "Cloudinary credentials incomplete. Uploads disabled (missing: %s)",
            ", ".join(missing),
        )
        return False, None, {}
    else:
        return False, None, {}

    try:
        import cloudinary  # pragma: no cover - optional dependency
        from cloudinary import uploader as cloudinary_uploader
    except Exception as exc:  # pragma: no cover - exercised via unit tests
        logger.warning("Cloudinary SDK not available: %s", exc)
        return False, None, {}

    config_kwargs.setdefault("secure", True)
    try:
        if has_all_explicit and cloudinary_url:
            original_url = os.environ.pop("CLOUDINARY_URL", None)
            try:
                cloudinary.config(**config_kwargs)
            finally:
                if original_url is not None:
                    os.environ["CLOUDINARY_URL"] = original_url
        else:
            cloudinary.config(**config_kwargs)
    except Exception as exc:  # pragma: no cover - configuration errors logged
        logger.error("Failed to configure Cloudinary: %s", exc, exc_info=True)
        return False, None, {}

    extra_options: dict[str, Any] = {}
    if upload_folder:
        sanitized = upload_folder.strip("/")
        if sanitized:
            extra_options["folder"] = sanitized

    # Log a diagnostic summary so environments like Render can confirm the
    # credentials detected by the backend without exposing full secrets.
    try:
        display_cloud_name = config_kwargs.get("cloud_name")
        display_api_key = config_kwargs.get("api_key")

        if (not display_cloud_name or not display_api_key) and cloudinary_url:
            parsed = urlparse(cloudinary_url)
            if not display_cloud_name and parsed.hostname:
                display_cloud_name = parsed.hostname
            if not display_api_key and parsed.username:
                display_api_key = parsed.username

        log_segments: list[str] = []
        if display_cloud_name:
            log_segments.append(f"cloud_name='{display_cloud_name}'")
        if display_api_key:
            masked = _mask_sensitive_value(display_api_key)
            log_segments.append(f"api_key='{masked}'")
        if extra_options.get("folder"):
            log_segments.append(f"folder='{extra_options['folder']}'")

        if log_segments:
            logger.info("Cloudinary uploads enabled (%s).", ", ".join(log_segments))
        else:
            logger.info("Cloudinary uploads enabled.")
    except Exception:  # pragma: no cover - logging helpers should not fail init
        logger.debug("Unable to log Cloudinary diagnostic summary", exc_info=True)

    return True, cloudinary_uploader, extra_options


CLOUDINARY_ENABLED: bool | None = None
uploader = None
CLOUDINARY_UPLOAD_OPTIONS: dict[str, Any] = {}
_CLOUDINARY_DISABLED_REASON: str | None = None
_CLOUDINARY_CONFIG_FINGERPRINT: tuple[str | None, str | None, str | None, str | None, str | None] | None = None


def _current_cloudinary_fingerprint() -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """Return a tuple identifying the active Cloudinary configuration."""

    return (
        _clean_env_value("CLOUDINARY_URL"),
        _clean_env_value("CLOUDINARY_CLOUD_NAME"),
        _clean_env_value("CLOUDINARY_API_KEY"),
        _clean_env_value("CLOUDINARY_API_SECRET"),
        _clean_env_value("CLOUDINARY_UPLOAD_FOLDER"),
    )


def _ensure_cloudinary_initialized(force: bool = False) -> None:
    """Initialise Cloudinary configuration when first accessed."""

    global CLOUDINARY_ENABLED
    global uploader
    global CLOUDINARY_UPLOAD_OPTIONS
    global _CLOUDINARY_DISABLED_REASON
    global _CLOUDINARY_CONFIG_FINGERPRINT

    if force:
        CLOUDINARY_ENABLED = None
        uploader = None
        CLOUDINARY_UPLOAD_OPTIONS = {}
        _CLOUDINARY_DISABLED_REASON = None
        _CLOUDINARY_CONFIG_FINGERPRINT = None

    fingerprint = _current_cloudinary_fingerprint()

    if CLOUDINARY_ENABLED is not None and fingerprint == _CLOUDINARY_CONFIG_FINGERPRINT:
        return

    enabled, configured_uploader, options = _init_cloudinary()
    CLOUDINARY_ENABLED = enabled
    uploader = configured_uploader
    CLOUDINARY_UPLOAD_OPTIONS = options
    _CLOUDINARY_CONFIG_FINGERPRINT = fingerprint


def refresh_cloudinary_configuration() -> None:
    """Force Cloudinary to pick up new credentials from env or app config."""

    _ensure_cloudinary_initialized(force=True)


_ensure_cloudinary_initialized()


def _disable_cloudinary(reason: str) -> None:
    """Disable Cloudinary uploads for the remainder of the process."""

    global CLOUDINARY_ENABLED, _CLOUDINARY_DISABLED_REASON

    if CLOUDINARY_ENABLED is None:
        _ensure_cloudinary_initialized()

    if not CLOUDINARY_ENABLED:
        return

    CLOUDINARY_ENABLED = False
    _CLOUDINARY_DISABLED_REASON = reason
    message = f"Cloudinary uploads disabled: {reason}"
    if has_app_context():
        current_app.logger.warning(message)
    else:
        logger.warning(message)

# Google Cloud Storage can be optionally disabled (e.g., when billing is off).
GCS_ENABLED = os.environ.get("GCS_ENABLED", "false").lower() == "true"
VERCEL_BLOB_RW_TOKEN = os.environ.get("VERCEL_BLOB_RW_TOKEN")
VERCEL_BLOB_API = "https://api.vercel.com/v2/blob"

if GCS_ENABLED:
    from google.cloud import storage
else:  # pragma: no cover - avoid import errors when disabled
    storage = None

BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME", "chatboc-files")
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB


def _get_gcs_client():
    """Initializes and returns a GCS client."""
    # This could be extended with more robust credential handling if needed
    return storage.Client()


def get_thumb_filename(original_filename: str) -> str:
    """Generates a predictable thumbnail filename from an original filename."""
    base, _ = os.path.splitext(original_filename)
    return f"{base}_thumb.webp"


def _normalize_mime(mime: str | None) -> str:
    """Return a normalized MIME type without parameters."""

    if not mime:
        return ""

    return mime.split(";", 1)[0].strip().lower()


def resolve_attachment_thumb_url(
    *,
    file_url: str | None,
    filename: str,
    mime_type: str | None,
    meta: dict | None = None,
) -> tuple[str | None, dict]:
    """Determine the most accurate thumbnail URL for an attachment.

    The widget relies on ``thumbUrl`` to render previews.  For audio and other
    non-visual files we must return the original file URL instead of pointing to
    a non-existent ``*_thumb.webp`` asset.  When a generated thumbnail exists we
    keep the previous behaviour so cached files (GCS/local) continue to work.
    """

    meta_data = dict(meta) if isinstance(meta, dict) else {}
    thumb_url = meta_data.get("url")
    if thumb_url:
        return thumb_url, meta_data

    normalized_mime = _normalize_mime(mime_type)
    supports_generated_thumb = (
        normalized_mime.startswith("image/") or normalized_mime == "application/pdf"
    )

    candidate_url: str | None = None
    if supports_generated_thumb:
        thumb_filename = get_thumb_filename(filename)
        if GCS_ENABLED:
            candidate_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{thumb_filename}"
        elif file_url:
            candidate_url = os.path.join(
                os.path.dirname(file_url), thumb_filename
            ).replace("\\", "/")

    if not candidate_url:
        candidate_url = file_url

    if candidate_url and "url" not in meta_data:
        meta_data["url"] = candidate_url

    return candidate_url, meta_data


def _save_to_local(
    original_filename: str,
    file_bytes: bytes,
    unique_name: str,
    mimetype: str,
    thumbnail_bytes: bytes | None,
    thumb_meta: dict | None,
    *,
    entity_subdir: str | None = None,
) -> dict:
    """Save files to the local filesystem when GCS is unavailable."""
    base_dir = _resolve_local_upload_base()
    entity_dir = _determine_fallback_subdir(entity_subdir)
    upload_dir = os.path.join(base_dir, entity_dir) if entity_dir else base_dir
    if entity_dir:
        logger.info(
            "Local storage fallback active for entity folder '%s'", entity_dir
        )
    os.makedirs(upload_dir, exist_ok=True)

    retention_days = current_app.config.get("LOCAL_UPLOAD_RETENTION_DAYS", 15)
    try:
        retention_days = int(retention_days)
    except (TypeError, ValueError):
        retention_days = 15
    _purge_old_files(upload_dir, retention_days)

    original_path = os.path.join(upload_dir, unique_name)
    with open(original_path, "wb") as f:
        f.write(file_bytes)
    thumb_url = None
    thumb_relative_url = None
    if thumbnail_bytes and thumb_meta:
        thumb_filename = get_thumb_filename(unique_name)
        thumb_path = os.path.join(upload_dir, thumb_filename)
        with open(thumb_path, "wb") as f:
            f.write(thumbnail_bytes)
        rel_thumb = os.path.relpath(thumb_path, current_app.root_path)
        thumb_relative_url = "/" + rel_thumb.replace(os.sep, "/")
        thumb_url = thumb_relative_url
        thumb_meta["url"] = thumb_url

    rel_path = os.path.relpath(original_path, current_app.root_path)
    relative_url = "/" + rel_path.replace(os.sep, "/")

    original_url = relative_url
    base_url, base_host = _normalize_base_url(_get_request_base_url())

    if not base_url and has_request_context():
        fallback_url, fallback_host = _normalize_base_url(request.url_root)
        base_url, base_host = fallback_url, fallback_host

    config_base_url, config_host = _normalize_base_url(current_app.config.get("BACKEND_URL"))
    if config_base_url:
        if not base_url:
            base_url, base_host = config_base_url, config_host
        elif base_host and _is_private_host(base_host):
            if not config_host or not _is_private_host(config_host):
                base_url, base_host = config_base_url, config_host

    if base_url:
        final_base_url, final_base_host = _normalize_base_url(base_url)
        if final_base_host and _is_private_host(final_base_host):
            public_domain = os.getenv("PUBLIC_ROOT_DOMAIN", "chatboc.ar")
            logger.warning(
                "Detected private/localhost base URL '%s', forcing public URL using domain '%s'.",
                base_url,
                public_domain,
            )
            base_url = f"https://{public_domain}"

        original_url = urljoin(f"{base_url}/", relative_url.lstrip("/"))
        if thumb_relative_url:
            thumb_url = urljoin(f"{base_url}/", thumb_relative_url.lstrip("/"))
            if thumb_meta:
                thumb_meta["url"] = thumb_url
    elif has_request_context():
        fallback_base = request.url_root.rstrip('/')
        original_url = f"{fallback_base}{relative_url}"
        if thumb_url:
            thumb_url = f"{fallback_base}{thumb_url}"
            if thumb_meta:
                thumb_meta["url"] = thumb_url

    return {
        "unique_name": unique_name,
        "original_url": original_url,
        "size": len(file_bytes),
        "original_name": original_filename,
        "mimetype": mimetype,
        "thumb_meta": thumb_meta,
        "thumbUrl": thumb_url,
        "path": os.path.realpath(original_path),
    }


def _save_to_vercel_blob(
    original_filename: str,
    file_bytes: bytes,
    unique_name: str,
    mimetype: str,
    thumbnail_bytes: bytes | None,
    thumb_meta: dict | None,
) -> dict | None:
    """Save files to Vercel Blob storage when configured."""
    if not VERCEL_BLOB_RW_TOKEN:
        return None

    headers = {
        "Authorization": f"Bearer {VERCEL_BLOB_RW_TOKEN}",
        "x-vercel-filename": unique_name,
        "Content-Type": mimetype,
    }
    resp = requests.post(VERCEL_BLOB_API, headers=headers, data=file_bytes)
    resp.raise_for_status()
    original_url = resp.json().get("url")
    thumb_url = None
    if thumbnail_bytes and thumb_meta:
        headers["x-vercel-filename"] = get_thumb_filename(unique_name)
        headers["Content-Type"] = "image/webp"
        resp_thumb = requests.post(
            VERCEL_BLOB_API, headers=headers, data=thumbnail_bytes
        )
        resp_thumb.raise_for_status()
        thumb_url = resp_thumb.json().get("url")
        thumb_meta["url"] = thumb_url

    return {
        "unique_name": unique_name,
        "original_url": original_url,
        "size": len(file_bytes),
        "original_name": original_filename,
        "mimetype": mimetype,
        "thumb_meta": thumb_meta,
        "thumbUrl": thumb_url,
    }


def _save_to_cloudinary(
    original_filename: str,
    file_bytes: bytes,
    unique_name: str,
    mimetype: str,
    thumbnail_bytes: bytes | None,
    thumb_meta: dict | None,
) -> dict | None:
    """Upload files to Cloudinary when enabled.

    Returns ``None`` if the upload fails so callers can gracefully fall back to
    the next configured storage backend.
    """
    _ensure_cloudinary_initialized()

    if not CLOUDINARY_ENABLED or uploader is None:
        return None

    try:  # pragma: no cover - exercised via unit tests with mocks
        file_obj = io.BytesIO(file_bytes)
        upload_kwargs = {"public_id": unique_name, "resource_type": "auto"}
        if CLOUDINARY_UPLOAD_OPTIONS:
            upload_kwargs.update(CLOUDINARY_UPLOAD_OPTIONS)
        upload_result = uploader.upload(file_obj, **upload_kwargs)
        original_url = upload_result.get("secure_url") or upload_result.get("url")

        thumb_url = None
        if thumbnail_bytes and thumb_meta:
            thumb_id = get_thumb_filename(unique_name)
            thumb_kwargs = {
                "public_id": thumb_id,
                "resource_type": "image",
                "format": "webp",
            }
            if CLOUDINARY_UPLOAD_OPTIONS:
                thumb_kwargs.update(CLOUDINARY_UPLOAD_OPTIONS)
            thumb_res = uploader.upload(
                io.BytesIO(thumbnail_bytes),
                **thumb_kwargs,
            )
            thumb_url = thumb_res.get("secure_url") or thumb_res.get("url")
            thumb_meta["url"] = thumb_url

        return {
            "unique_name": unique_name,
            "original_url": original_url,
            "size": len(file_bytes),
            "original_name": original_filename,
            "mimetype": mimetype,
            "thumb_meta": thumb_meta,
            "thumbUrl": thumb_url,
        }
    except Exception as exc:  # pragma: no cover - the behaviour is tested via mocks
        log_kwargs = {"exc_info": True}
        message = "Cloudinary upload failed for %s: %s"
        if has_app_context():
            current_app.logger.error(message, original_filename, exc, **log_kwargs)
        else:
            logger.error(message, original_filename, exc, **log_kwargs)

        error_message = str(exc).lower()
        auth_errors = (
            "unknown api key",
            "invalid api key",
            "must specify api key",
            "api key is invalid",
            "unauthorized",
            "must supply api_secret",
            "must supply api secret",
        )
        if any(token in error_message for token in auth_errors):
            reason = "authentication error"
            if "unknown api key" in error_message:
                reason = "unknown api key"
            elif "must supply api_secret" in error_message or "must supply api secret" in error_message:
                reason = "missing api secret"
            _disable_cloudinary(reason)

        return None


def upload_to_gcs(file_storage, kind: str = "attachments") -> dict | None:
    """Upload a file to the configured storage backend.

    Order of preference:
    1. Cloudflare R2 (Primary)
    2. Cloudinary (Fallback)
    3. GCS (Legacy/Secondary)
    4. Local Filesystem (Dev/Last Resort)

    Args:
        file_storage: The ``FileStorage`` object from Flask request.
        kind: The subfolder or type of upload (e.g. 'catalogos', 'logos', 'attachments')

    Returns:
        A dictionary containing the file's metadata (unique name, URL, size, etc.) or
        ``None`` if the upload fails.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_filename = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_filename}"

    file_storage.seek(0)
    file_bytes = file_storage.read()

    # 1. R2 Upload Strategy
    try:
        # Determine context/owner
        owner = getattr(g, 'current_user', None) or getattr(g, 'owner_user', None)
        if not owner and has_app_context() and hasattr(g, 'viewer'):
            owner = g.viewer

        key_prefix = _determine_r2_key_prefix(owner, kind=kind)
        r2_key = f"{key_prefix}/{unique_name}"

        file_stream_r2 = io.BytesIO(file_bytes)
        r2_url = r2_service.upload_file_with_key(file_stream_r2, r2_key, file_storage.mimetype)

        if r2_url:
            return {
                "unique_name": unique_name,
                "public_url": r2_url,
                "size": len(file_bytes),
                "original_name": original_filename,
                "mimetype": file_storage.mimetype,
            }
        else:
            logger.warning(f"R2 upload failed for {original_filename}, attempting fallback.")
    except Exception as e:
        logger.error(f"R2 Upload Exception: {e}", exc_info=True)

    # 2. Cloudinary Fallback
    _ensure_cloudinary_initialized()

    if CLOUDINARY_ENABLED:
        result = _save_to_cloudinary(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            None,
            None,
        )
        if result:
            return {
                "unique_name": result["unique_name"],
                "public_url": result["original_url"],
                "size": result["size"],
                "original_name": result["original_name"],
                "mimetype": result["mimetype"],
            }
        warning_msg = "Falling back to secondary storage after Cloudinary upload failure."
        if has_app_context():
            current_app.logger.warning(warning_msg)
        else:
            logger.warning(warning_msg)

    # If GCS is disabled, store locally and return its metadata
    if not GCS_ENABLED:
        local = _save_to_local(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            None,
            None,
        )
        return {
            "unique_name": local["unique_name"],
            "public_url": local["original_url"],
            "size": local["size"],
            "original_name": local["original_name"],
            "mimetype": local["mimetype"],
            "path": local.get("path"),
        }

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(unique_name)

        blob.upload_from_string(file_bytes, content_type=file_storage.mimetype)

        if blob.size > MAX_FILE_SIZE:
            current_app.logger.warning(
                f"User uploaded a file larger than MAX_FILE_SIZE: {original_filename} ({blob.size} bytes)"
            )
            blob.delete()
            return None

        return {
            "unique_name": unique_name,
            "public_url": blob.public_url,
            "size": blob.size,
            "original_name": original_filename,
            "mimetype": file_storage.mimetype,
        }
    except Exception as e:
        current_app.logger.error(
            f"Error uploading file {original_filename} to GCS: {e}", exc_info=True
        )
        return None


def _determine_r2_key_prefix(owner_user, kind: str = "attachments") -> str:
    """
    Determine the R2 key prefix (folder structure) based on the owner.
    Format: <type>/<slug>/<kind>/<filename>
    User request: pymes/<tenant_slug>/... or municipios/<tenant_slug>/...
    """
    if not owner_user:
        return f"uploads/anonymous/{kind}"

    # Try to find tenant profile slug via relationships or IDs
    # If we have a direct tenant relationship:
    tenant = getattr(owner_user, 'tenant', None) # If User has 'tenant'
    if not tenant and getattr(owner_user, 'tenant_profile_pyme', None):
        tenant = owner_user.tenant_profile_pyme
    if not tenant and getattr(owner_user, 'tenant_profile_municipio', None):
        tenant = owner_user.tenant_profile_municipio

    # Or maybe via the g.tenant_profile if set by middleware
    if has_app_context() and hasattr(g, 'tenant_profile') and g.tenant_profile:
        tenant = g.tenant_profile

    slug = "unknown"
    prefix_type = "uploads"

    if tenant:
        slug = tenant.slug
        if tenant.tipo == 'municipio':
            prefix_type = 'municipios'
        else:
            prefix_type = 'pymes'
    else:
        # Fallback logic if no full tenant profile is loaded but IDs exist
        if getattr(owner_user, 'municipio_id', None):
            prefix_type = 'municipios'
            slug = str(owner_user.municipio_id) # Ideally fetch slug, but ID is safe fallback
        elif getattr(owner_user, 'pyme_id', None):
            prefix_type = 'pymes'
            slug = str(owner_user.pyme_id)
        elif getattr(owner_user, 'tipo_chat', None) == 'municipio':
            prefix_type = 'municipios'
            slug = getattr(owner_user, 'tenant_slug', str(owner_user.id))
        elif getattr(owner_user, 'tipo_chat', None) == 'pyme':
            prefix_type = 'pymes'
            slug = getattr(owner_user, 'tenant_slug', str(owner_user.id))

    # Sanitize kind to prevent directory traversal or weird chars
    safe_kind = _sanitize_path_segment(kind) or "attachments"

    return f"{prefix_type}/{slug}/{safe_kind}"


def guardar_adjunto_y_thumbnail(file_storage, kind: str = "attachments") -> dict | None:
    """Upload a file and its generated thumbnail to storage.

    Order of preference:
    1. Cloudflare R2 (Primary)
    2. Cloudinary (Fallback)
    3. GCS (Legacy/Secondary)
    4. Local Filesystem (Dev/Last Resort)

    Args:
        file_storage: The ``FileStorage`` object from the request.
        kind: The subfolder or type of upload (e.g. 'catalogos', 'logos', 'attachments')

    Returns:
        A dictionary with the file URL and thumbnail metadata, or ``None`` on failure.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_filename = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_filename}"

    # Rewind stream to read for validation and thumbnailing
    file_storage.seek(0)
    file_bytes = file_storage.read()

    if len(file_bytes) > MAX_FILE_SIZE:
        current_app.logger.warning(
            f"File '{original_filename}' exceeds max size of {MAX_FILE_SIZE} bytes."
        )
        return None

    # Create a new stream for thumbnail generation
    file_stream_for_thumb = io.BytesIO(file_bytes)
    thumbnail_bytes, thumb_meta = generar_thumbnail(
        file_stream_for_thumb, file_storage.mimetype
    )

    # 1. R2 Upload Strategy
    try:
        # Determine context/owner
        owner = getattr(g, 'current_user', None) or getattr(g, 'owner_user', None)
        # Use g.viewer from app.py logic if available
        if not owner and has_app_context() and hasattr(g, 'viewer'):
            owner = g.viewer

        key_prefix = _determine_r2_key_prefix(owner, kind=kind)
        r2_key = f"{key_prefix}/{unique_name}"

        # Reset stream for R2
        file_stream_r2 = io.BytesIO(file_bytes)
        r2_url = r2_service.upload_file_with_key(file_stream_r2, r2_key, file_storage.mimetype)

        if r2_url:
            # Upload thumbnail to R2 if exists
            thumb_url = None
            if thumbnail_bytes and thumb_meta:
                thumb_filename = get_thumb_filename(unique_name)
                r2_thumb_key = f"{key_prefix}/{thumb_filename}"
                thumb_url = r2_service.upload_file_with_key(
                    io.BytesIO(thumbnail_bytes),
                    r2_thumb_key,
                    "image/webp"
                )
                if thumb_url:
                    thumb_meta["url"] = thumb_url

            return {
                "unique_name": unique_name,
                "original_url": r2_url,
                "size": len(file_bytes),
                "original_name": original_filename,
                "mimetype": file_storage.mimetype,
                "thumb_meta": thumb_meta,
                "thumbUrl": thumb_url,
            }
        else:
            logger.warning(f"R2 upload failed for {original_filename}, attempting fallback.")

    except Exception as e:
        logger.error(f"R2 Upload Exception: {e}", exc_info=True)
        # Continue to fallbacks

    # 2. Cloudinary Fallback
    _ensure_cloudinary_initialized()

    if CLOUDINARY_ENABLED:
        cloudinary_result = _save_to_cloudinary(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )
        if cloudinary_result:
            return cloudinary_result
        warning_msg = "Falling back to alternative storage after Cloudinary upload failure."
        if has_app_context():
            current_app.logger.warning(warning_msg)
        else:
            logger.warning(warning_msg)

    if VERCEL_BLOB_RW_TOKEN:
        return _save_to_vercel_blob(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )

    if not GCS_ENABLED:
        return _save_to_local(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )

    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(BUCKET_NAME)

        blob_original = bucket.blob(unique_name)
        blob_original.upload_from_string(file_bytes, content_type=file_storage.mimetype)

        thumb_url = None
        if thumbnail_bytes and thumb_meta:
            thumb_filename = get_thumb_filename(unique_name)
            blob_thumb = bucket.blob(thumb_filename)
            blob_thumb.upload_from_string(thumbnail_bytes, content_type="image/webp")
            thumb_url = blob_thumb.public_url
            thumb_meta["url"] = thumb_url
            current_app.logger.info(f"Thumbnail uploaded to {thumb_url}")

        return {
            "unique_name": unique_name,
            "original_url": blob_original.public_url,
            "size": len(file_bytes),
            "original_name": original_filename,
            "mimetype": file_storage.mimetype,
            "thumb_meta": thumb_meta,
            "thumbUrl": thumb_url,
        }

    except Exception as e:
        current_app.logger.error(
            f"Error in guardar_adjunto_y_thumbnail for {original_filename}: {e}",
            exc_info=True,
        )
        current_app.logger.warning(
            "Falling back to local file storage for attachments."
        )
        return _save_to_local(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )


def upload_file_from_url(url: str, kind: str = "attachments") -> dict | None:
    """
    Downloads a file from a URL and uploads it to the configured storage.
    """
    if not url:
        return None

    try:
        response = requests.get(url, stream=True)
        response.raise_for_status()

        # Get filename from URL
        filename = url.split("/")[-1].split("?")[0] or "attachment.jpg"
        if not os.path.splitext(filename)[1]:
            # If no extension, try to guess from content type
            content_type = response.headers.get("content-type", "image/jpeg")
            ext = content_type.split("/")[-1]
            filename = f"{filename}.{ext}"

        file_stream = io.BytesIO(response.content)

        # Create a FileStorage-like object
        from werkzeug.datastructures import FileStorage # Import here if needed or at top
        file_storage = FileStorage(
            stream=file_stream,
            filename=filename,
            content_type=response.headers.get(
                "content-type", "application/octet-stream"
            ),
        )

        current_app.logger.info(f"Uploading file from URL: {url} as {filename}")
        return guardar_adjunto_y_thumbnail(file_storage, kind=kind)

    except requests.exceptions.RequestException as e:
        current_app.logger.error(
            f"Failed to download file from URL {url}: {e}", exc_info=True
        )
        return None
    except Exception as e:
        current_app.logger.error(
            f"Failed to upload file from URL {url}: {e}", exc_info=True
        )
        return None
