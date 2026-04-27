from flask import Blueprint, jsonify

v2_health_bp = Blueprint("v2_health", __name__, url_prefix="/api/v2")


@v2_health_bp.route('/health', methods=['GET'])
def health_v2():
    return jsonify({"ok": True, "version": "v2"})
