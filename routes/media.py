from flask import Blueprint, send_from_directory, current_app
import os

media_bp = Blueprint("media_bp", __name__, url_prefix="/media")


@media_bp.route("/<path:filename>")
def media(filename: str):
    """Serve files from a persistent data directory.

    Deployments mount a volume at ``/data`` (or configure ``DATA_DIR``)
    so uploaded files survive redeploys.  For local development we
    fallback to the repository's bundled ``data/`` folder when the file
    is not found in the persistent path.
    """
    data_dir = current_app.config.get("DATA_DIR", os.environ.get("DATA_DIR", "/data"))
    repo_dir = os.path.join(current_app.root_path, "data")

    if os.path.exists(os.path.join(data_dir, filename)):
        return send_from_directory(data_dir, filename)

    return send_from_directory(repo_dir, filename)
