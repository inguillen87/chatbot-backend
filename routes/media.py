from flask import Blueprint, send_from_directory, current_app
import os

from services.config_loader import BASE_DATA_PATH
from services.media_cache_policy import cache_control_for_key

media_bp = Blueprint("media_bp", __name__, url_prefix="/media")


def _serve_media_file(directory: str, filename: str):
    response = send_from_directory(directory, filename)
    response.headers["Cache-Control"] = cache_control_for_key(filename, response.mimetype)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@media_bp.route("/<path:filename>")
def media(filename: str):
    """Serve files from a persistent data directory.

    Deployments mount a volume at ``/data`` (or configure ``DATA_DIR``)
    so uploaded files survive redeploys.  For local development we
    fallback to the repository's bundled ``data/`` folder when the file
    is not found in the persistent path.
    """
    data_dir = current_app.config.get("DATA_DIR", BASE_DATA_PATH)
    repo_dir = os.path.join(current_app.root_path, "data")

    if os.path.exists(os.path.join(data_dir, filename)):
        return _serve_media_file(data_dir, filename)

    return _serve_media_file(repo_dir, filename)
