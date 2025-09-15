import os
import uuid
import io
import requests
from flask import current_app
from werkzeug.utils import secure_filename
from werkzeug.datastructures import FileStorage
from services.thumbnail_service import generar_thumbnail

# Optional Cloudinary storage
CLOUDINARY_URL = os.environ.get("CLOUDINARY_URL")
CLOUDINARY_ENABLED = bool(CLOUDINARY_URL)
if CLOUDINARY_ENABLED:  # pragma: no cover - optional dependency
    import cloudinary
    from cloudinary import uploader

    cloudinary.config(cloudinary_url=CLOUDINARY_URL)
else:  # pragma: no cover - avoid import when disabled
    uploader = None

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
    original_url = "/" + rel_path.replace(os.sep, "/")

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
) -> dict:
    """Upload files to Cloudinary when enabled."""
    file_obj = io.BytesIO(file_bytes)
    upload_result = uploader.upload(
        file_obj, public_id=unique_name, resource_type="auto"
    )
    original_url = upload_result.get("secure_url") or upload_result.get("url")

    thumb_url = None
    if thumbnail_bytes and thumb_meta:
        thumb_id = get_thumb_filename(unique_name)
        thumb_res = uploader.upload(
            io.BytesIO(thumbnail_bytes),
            public_id=thumb_id,
            resource_type="image",
            format="webp",
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
        return {
            "unique_name": result["unique_name"],
            "public_url": result["original_url"],
            "size": result["size"],
            "original_name": result["original_name"],
            "mimetype": result["mimetype"],
        }

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
        return _save_to_cloudinary(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )

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
