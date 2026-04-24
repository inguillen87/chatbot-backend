"""Endpoints públicos para listar subastas activas.

Estas rutas permiten al widget y al frontend obtener información de subastas
sin requerir autenticación. La fuente actual es un archivo JSON en ``data/subastas.json``
para simplificar el bootstrap del feature.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from services.subastas import listar_subastas_activas, obtener_subasta_por_id

subastas_bp = Blueprint("subastas", __name__, url_prefix="/api/subastas")


@subastas_bp.route("", methods=["GET"])
def listar_subastas():
    """Devuelve la lista de subastas activas ordenadas por fecha de cierre."""

    return jsonify({"subastas": listar_subastas_activas()})


@subastas_bp.route("/<subasta_id>", methods=["GET"])
def detalle_subasta(subasta_id: str):
    """Obtiene el detalle de una subasta por ``id`` o ``titulo``."""

    subasta = obtener_subasta_por_id(subasta_id)
    if not subasta:
        return jsonify({"error": "Subasta no encontrada"}), 404

    return jsonify(subasta)
