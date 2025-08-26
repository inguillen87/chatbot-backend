from flask import Blueprint, render_template, current_app
import os
from utils.auth_helpers import token_requerido, admin_o_empleado_requerido


estadisticas_bp = Blueprint("estadisticas", __name__, url_prefix="/estadisticas")


@estadisticas_bp.route("/mapa_calor")
@token_requerido
@admin_o_empleado_requerido
def mapa_calor(current_user):
    """Renderiza el mapa de calor."""
    maptiler_key = current_app.config.get("MAPTILER_KEY") or os.getenv("VITE_MAPTILER_KEY", "")
    return render_template("estadisticas.html", maptiler_key=maptiler_key)
