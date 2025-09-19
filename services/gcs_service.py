import logging
import os
import uuid
import io
from urllib.parse import urlparse

import requests
from flask import current_app, has_app_context, request
from werkzeug.utils import secure_filename
from services.thumbnail_service import generar_thumbnail

logger = logging.getLogger(__name__)


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


def _init_cloudinary():  # pragma: no cover - thin wrapper validated via tests
    """Return a tuple ``(enabled, uploader, extra_options)`` based on env vars."""

    cloudinary_url = os.environ.get("CLOUDINARY_URL")
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME")
    api_key = os.environ.get("CLOUDINARY_API_KEY")
    api_secret = os.environ.get("CLOUDINARY_API_SECRET")
    upload_folder = os.environ.get("CLOUDINARY_UPLOAD_FOLDER")

    config_kwargs = {}
    if cloudinary_url:
        config_kwargs["cloudinary_url"] = cloudinary_url
    else:
        provided_keys = [cloud_name, api_key, api_secret]
        if any(provided_keys) and not all(provided_keys):
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
        if all(provided_keys):
            config_kwargs.update(
                cloud_name=cloud_name,
                api_key=api_key,
                api_secret=api_secret,
            )

    if not config_kwargs:
        return False, None, {}

    try:
        import cloudinary  # pragma: no cover - optional dependency
        from cloudinary import uploader as cloudinary_uploader
    except Exception as exc:  # pragma: no cover - exercised via unit tests
        logger.warning("Cloudinary SDK not available: %s", exc)
        return False, None, {}

    config_kwargs.setdefault("secure", True)
    try:
        cloudinary.config(**config_kwargs)
    except Exception as exc:  # pragma: no cover - configuration errors logged
        logger.error("Failed to configure Cloudinary: %s", exc, exc_info=True)
        return False, None, {}

    extra_options = {}
    if upload_folder:
        sanitized = upload_folder.strip().strip("/")
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


CLOUDINARY_ENABLED, uploader, CLOUDINARY_UPLOAD_OPTIONS = _init_cloudinary()
_CLOUDINARY_DISABLED_REASON: str | None = None


def _disable_cloudinary(reason: str) -> None:
    """Disable Cloudinary uploads for the remainder of the process."""

    global CLOUDINARY_ENABLED, _CLOUDINARY_DISABLED_REASON

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


def _save_to_local(
    original_filename: str,
    file_bytes: bytes,
    unique_name: str,
    mimetype: str,
    thumbnail_bytes: bytes | None,
    thumb_meta: dict | None,
) -> dict:
    """Save files to the local filesystem when GCS is unavailable."""
    upload_dir = current_app.config.get(
        "LOCAL_UPLOAD_FOLDER",
        os.path.join(current_app.root_path, "static", "uploads"),
    )
    os.makedirs(upload_dir, exist_ok=True)

    original_path = os.path.join(upload_dir, unique_name)
    with open(original_path, "wb") as f:
        f.write(file_bytes)
    thumb_url = None
    if thumbnail_bytes and thumb_meta:
        thumb_filename = get_thumb_filename(unique_name)
        thumb_path = os.path.join(upload_dir, thumb_filename)
        with open(thumb_path, "wb") as f:
            f.write(thumbnail_bytes)
        rel_thumb = os.path.relpath(thumb_path, current_app.root_path)
        thumb_url = "/" + rel_thumb.replace(os.sep, "/")
        thumb_meta["url"] = thumb_url

    rel_path = os.path.relpath(original_path, current_app.root_path)
    relative_url = "/" + rel_path.replace(os.sep, "/")

    # Generate an absolute URL if in a request context
    original_url = relative_url
    if has_app_context() and request:
        base_url = request.url_root.rstrip('/')
        original_url = f"{base_url}{relative_url}"
        if thumb_url:
            thumb_url = f"{base_url}{thumb_url}"
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
        )
        if any(token in error_message for token in auth_errors):
            reason = "authentication error"
            if "unknown api key" in error_message:
                reason = "unknown api key"
            _disable_cloudinary(reason)

        return None


def upload_to_gcs(file_storage) -> dict | None:
    """Upload a file to the configured storage backend.

    If ``GCS_ENABLED`` is false, the file is saved to the local filesystem instead of
    Google Cloud Storage.

    Args:
        file_storage: The ``FileStorage`` object from Flask request.

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


def guardar_adjunto_y_thumbnail(file_storage) -> dict | None:
    """Upload a file and its generated thumbnail to storage.

    Uses GCS when enabled, otherwise falls back to local filesystem storage.

    Args:
        file_storage: The ``FileStorage`` object from the request.

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


def upload_file_from_url(url: str) -> dict | None:
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
        file_storage = FileStorage(
            stream=file_stream,
            filename=filename,
            content_type=response.headers.get(
                "content-type", "application/octet-stream"
            ),
        )

        current_app.logger.info(f"Uploading file from URL: {url} as {filename}")
        return guardar_adjunto_y_thumbnail(file_storage)

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
