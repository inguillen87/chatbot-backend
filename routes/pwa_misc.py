"""Misc endpoints for the PWA experience."""

from flask import Blueprint, jsonify

from utils.auth_helpers import _set_anon_cookie, get_or_create_anon_id


pwa_misc_bp = Blueprint("pwa_misc", __name__, url_prefix="/api/pwa")


@pwa_misc_bp.route("/anon-id", methods=["GET", "OPTIONS"])
def provide_anon_id():
    """Return or create the anon_id for the current visitor."""

    anon_id = get_or_create_anon_id()
    resp = jsonify({"anonId": anon_id})
    resp.headers.setdefault("X-Anon-Id", anon_id)
    resp.headers.setdefault("Anon-Id", anon_id)
    return _set_anon_cookie(resp, anon_id)
