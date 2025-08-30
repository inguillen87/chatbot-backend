from flask import Blueprint, request, jsonify
from services.estacionamiento_service import consultar_ocupacion

bp_est = Blueprint("estacionamiento", __name__)

@bp_est.route("/estacionamiento", methods=["GET"])
def get_est():
    q = request.args.get("q")
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    # Preserve zero coordinates by checking explicitly for None
    ubic = {"lat": lat, "lon": lon} if lat is not None and lon is not None else q
    res = consultar_ocupacion(ubic)
    return jsonify(res)
