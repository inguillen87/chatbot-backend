import os
import uuid
from flask import current_app
from google.cloud import storage
from werkzeug.utils import secure_filename

BUCKET_NAME = "chatboc-files"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

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
