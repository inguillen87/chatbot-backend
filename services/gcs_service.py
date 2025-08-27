import os
import uuid
from flask import current_app
from google.cloud import storage
from werkzeug.utils import secure_filename
import io
from services.thumbnail_service import generar_thumbnail

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


def _save_to_local(original_filename: str, file_bytes: bytes, unique_name: str,
                   mimetype: str, thumbnail_bytes: bytes | None,
                   thumb_meta: dict | None) -> dict:
    """Save files to the local filesystem when GCS is unavailable."""
    upload_dir = current_app.config.get(
        "LOCAL_UPLOAD_FOLDER",
        os.path.join(current_app.root_path, "static", "uploads"),
    )
    os.makedirs(upload_dir, exist_ok=True)

    original_path = os.path.join(upload_dir, unique_name)
    with open(original_path, "wb") as f:
        f.write(file_bytes)

    if thumbnail_bytes and thumb_meta:
        thumb_filename = get_thumb_filename(unique_name)
        thumb_path = os.path.join(upload_dir, thumb_filename)
        with open(thumb_path, "wb") as f:
            f.write(thumbnail_bytes)

    rel_path = os.path.relpath(original_path, current_app.root_path)
    original_url = "/" + rel_path.replace(os.sep, "/")

    return {
        "unique_name": unique_name,
        "original_url": original_url,
        "size": len(file_bytes),
        "original_name": original_filename,
        "mimetype": mimetype,
        "thumb_meta": thumb_meta,
    }

def upload_to_gcs(file_storage) -> dict | None:
    """
    Uploads a file to Google Cloud Storage and returns its metadata.

    Args:
        file_storage: The FileStorage object from Flask request.

    Returns:
        A dictionary containing the file's metadata (unique_name, public_url, size, etc.)
        or None if the upload fails.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_filename = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_filename}"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(unique_name)

        # Rewind the file stream before uploading
        file_storage.seek(0)
        blob.upload_from_file(file_storage, content_type=file_storage.mimetype)

        # Check file size after upload
        if blob.size > MAX_FILE_SIZE:
            current_app.logger.warning(f"User uploaded a file larger than MAX_FILE_SIZE: {original_filename} ({blob.size} bytes)")
            blob.delete()  # Clean up the oversized file
            return None

        return {
            "unique_name": unique_name,
            "public_url": blob.public_url,
            "size": blob.size,
            "original_name": original_filename,
            "mimetype": file_storage.mimetype,
        }
    except Exception as e:
        current_app.logger.error(f"Error uploading file {original_filename} to GCS: {e}", exc_info=True)
        return None

def guardar_adjunto_y_thumbnail(file_storage) -> dict | None:
    """
    Uploads a file and its generated thumbnail to GCS.

    Args:
        file_storage: The FileStorage object from the request.

    Returns:
        A dictionary with original file URL, and thumbnail metadata, or None on failure.
    """
    if not file_storage or not file_storage.filename:
        return None

    original_filename = secure_filename(file_storage.filename)
    unique_name = f"{uuid.uuid4().hex}_{original_filename}"

    # Rewind stream to read for validation and thumbnailing
    file_storage.seek(0)
    file_bytes = file_storage.read()

    if len(file_bytes) > MAX_FILE_SIZE:
        current_app.logger.warning(f"File '{original_filename}' exceeds max size of {MAX_FILE_SIZE} bytes.")
        return None

    # Create a new stream for thumbnail generation
    file_stream_for_thumb = io.BytesIO(file_bytes)
    thumbnail_bytes, thumb_meta = generar_thumbnail(file_stream_for_thumb, file_storage.mimetype)

    try:
        storage_client = _get_gcs_client()
        bucket = storage_client.bucket(BUCKET_NAME)

        # 1. Upload Original File
        blob_original = bucket.blob(unique_name)
        blob_original.upload_from_string(file_bytes, content_type=file_storage.mimetype)

        # 2. Upload Thumbnail if available
        if thumbnail_bytes and thumb_meta:
            thumb_filename = get_thumb_filename(unique_name)
            blob_thumb = bucket.blob(thumb_filename)
            blob_thumb.upload_from_string(thumbnail_bytes, content_type='image/webp')
            current_app.logger.info(f"Thumbnail uploaded to {blob_thumb.public_url}")

        return {
            "unique_name": unique_name,
            "original_url": blob_original.public_url,
            "size": len(file_bytes),
            "original_name": original_filename,
            "mimetype": file_storage.mimetype,
            "thumb_meta": thumb_meta
        }

    except Exception as e:
        current_app.logger.error(
            f"Error in guardar_adjunto_y_thumbnail for {original_filename}: {e}",
            exc_info=True,
        )
        current_app.logger.warning("Falling back to local file storage for attachments.")
        return _save_to_local(
            original_filename,
            file_bytes,
            unique_name,
            file_storage.mimetype,
            thumbnail_bytes,
            thumb_meta,
        )
