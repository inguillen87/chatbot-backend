from flask import Blueprint, request, jsonify
from services.estacionamiento_service import consultar_ocupacion

bp_est = Blueprint("estacionamiento", __name__)

@bp_est.route("/estacionamiento", methods=["GET"])
def get_est():
    q = request.args.get("q")
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    ubic = {"lat":lat,"lon":lon} if lat and lon else q
    res = consultar_ocupacion(ubic)
    return jsonify(res)
