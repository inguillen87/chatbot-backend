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
        'backendVersion': current_app.config['BACKEND_VERSION'],
        'frontendVersion': current_app.config['FRONTEND_VERSION'],
        # Add any other public-facing config vars the frontend might need
    })


@config_bp.route('/api/version', methods=['GET'])
def get_version_info():
    """Expose the deployed frontend/backend versions for health checks."""

    return jsonify({
        'frontend': current_app.config['FRONTEND_VERSION'],
        'backend': current_app.config['BACKEND_VERSION'],
    })


@config_bp.route('/api/config/maps', methods=['GET'])
def get_maps_config():
    """Expose map provider configuration for the frontend widget."""

    return jsonify({
        "google_maps_api_key": current_app.config.get("MAPS_API_KEY", ""),
        "default_provider": current_app.config.get("MAPS_DEFAULT_PROVIDER", "google"),
    })
