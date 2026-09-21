import json
from pathlib import Path

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


def _load_runtime_recovery_ui():
    """Read only explicit, deployment-owned public status copy; never app secrets."""
    path = Path(__file__).resolve().parents[1] / 'config' / 'runtime_recovery_ui.json'
    with path.open(encoding='utf-8') as stream:
        payload = json.load(stream)
    if (not isinstance(payload, dict)
            or payload.get('contract_version') != 'chatboc.runtime_recovery_ui.v1'
            or payload.get('scope') != 'platform'):
        raise ValueError('invalid_runtime_recovery_contract')
    return payload


@config_bp.route('/api/config/runtime-recovery', methods=['GET'])
def get_runtime_recovery_ui():
    """Publish technical platform copy, without user, tenant or business state."""
    try:
        response = jsonify(_load_runtime_recovery_ui())
    except (OSError, ValueError):
        response = jsonify({'error': 'runtime_recovery_config_unavailable'})
        response.status_code = 503
    response.headers['Cache-Control'] = 'no-store'
    return response


@config_bp.route('/api/config/maps', methods=['GET'])
def get_maps_config():
    """Expose map provider configuration for the frontend widget."""

    return jsonify({
        "google_maps_api_key": current_app.config.get("MAPS_API_KEY", ""),
        "default_provider": current_app.config.get("MAPS_DEFAULT_PROVIDER", "google"),
    })
