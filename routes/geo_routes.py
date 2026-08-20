from flask import Blueprint, jsonify


geo_bp = Blueprint("geo_bp", __name__, url_prefix="/api/geo")


@geo_bp.route("/polygons", methods=["GET"])
def get_polygons():
    """Retire the former synthetic polygon grid at the truth boundary.

    No official administrative boundary source is configured for this route.
    Returning an empty, stable 410 contract prevents generated geometry and
    random metrics from being presented as municipal evidence.
    """

    return (
        jsonify(
            {
                "contract_version": "geo.polygons.truth_boundary.v1",
                "status_code": 410,
                "reason_code": "official_geo_boundaries_unavailable",
                "retryable": False,
                "message": "No hay limites territoriales oficiales configurados.",
                "source_status": "not_configured",
                "truth_boundary": "no_synthetic_boundaries",
            }
        ),
        410,
    )
