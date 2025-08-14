from flask import Blueprint, jsonify, current_app

config_bp = Blueprint('config_bp', __name__)

@config_bp.route('/api/config', methods=['GET'])
def get_config():
    """
    Provides a public endpoint for the frontend to fetch necessary
    backend configuration, like the backend URL.
    """
    return jsonify({
        'backendUrl': current_app.config['BACKEND_URL'],
        'panelUrl': current_app.config['PANEL_URL'],
        # Add any other public-facing config vars the frontend might need
    })
