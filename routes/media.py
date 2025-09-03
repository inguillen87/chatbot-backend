from flask import Blueprint, send_from_directory, current_app
import os

media_bp = Blueprint('media_bp', __name__, url_prefix='/media')

@media_bp.route('/<path:filename>')
def media(filename: str):
    """Serve files stored under the local data directory.

    This allows the frontend to render images or documents using a simple
    /media/<path> URL, e.g. /media/archivos/imagen.png
    """
    data_dir = os.path.join(current_app.root_path, 'data')
    return send_from_directory(data_dir, filename)
