from flask import Blueprint, jsonify, request
from flask_login import current_user
from models import TenantProfile, User
from database import db
import math
import random

geo_bp = Blueprint('geo_bp', __name__, url_prefix='/api/geo')

def generate_grid_polygons(center_lat, center_lng, radius_km=2.0, grid_size=3):
    """
    Generates a synthetic grid of polygons around a central point.
    Returns a list of GeoJSON features.
    """
    features = []

    # Approx degrees per km
    # 1 deg lat ~= 111km
    # 1 deg lng ~= 111km * cos(lat)
    km_in_deg_lat = 1.0 / 111.0
    km_in_deg_lng = 1.0 / (111.0 * math.cos(math.radians(center_lat)))

    step_lat = (radius_km * 2 / grid_size) * km_in_deg_lat
    step_lng = (radius_km * 2 / grid_size) * km_in_deg_lng

    start_lat = center_lat - (radius_km * km_in_deg_lat)
    start_lng = center_lng - (radius_km * km_in_deg_lng)

    for i in range(grid_size):
        for j in range(grid_size):
            # Calculate cell bounds
            cell_lat_min = start_lat + (i * step_lat)
            cell_lat_max = start_lat + ((i + 1) * step_lat)
            cell_lng_min = start_lng + (j * step_lng)
            cell_lng_max = start_lng + ((j + 1) * step_lng)

            # Create Polygon coordinates (closed loop, counter-clockwise usually)
            # [lng, lat]
            poly_coords = [
                [cell_lng_min, cell_lat_min],
                [cell_lng_max, cell_lat_min],
                [cell_lng_max, cell_lat_max],
                [cell_lng_min, cell_lat_max],
                [cell_lng_min, cell_lat_min] # Close the loop
            ]

            # Synthetic data
            density = random.randint(10, 100)
            val = random.randint(100, 5000)

            # Simple color scale based on density
            color = "#00FF00" # Low
            if density > 40: color = "#FFFF00" # Med
            if density > 75: color = "#FF0000" # High

            feature = {
                "type": "Feature",
                "properties": {
                    "name": f"Zona {i}-{j}",
                    "density": density,
                    "value": val,
                    "fill": color
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [poly_coords]
                }
            }
            features.append(feature)

    return features

@geo_bp.route('/polygons', methods=['GET'])
def get_polygons():
    """
    Returns GeoJSON polygons dynamically generated around the tenant's location.
    This ensures the map visualization works regardless of the city.
    """
    tenant_id = request.args.get('tenant_id')

    # Resolve tenant
    tenant = None
    if tenant_id:
         tenant = db.session.get(TenantProfile, tenant_id)
    elif current_user.is_authenticated and current_user.tenant_id:
         tenant = db.session.get(TenantProfile, current_user.tenant_id)

    # Default Center (Buenos Aires Obelisco)
    center_lat = -34.6037
    center_lng = -58.3816

    if tenant:
        # Try to find location from the owner user (municipio or pyme)
        owner = None
        if tenant.municipio_id:
            owner = db.session.get(User, tenant.municipio_id)
        elif tenant.pyme_id:
            owner = db.session.get(User, tenant.pyme_id)

        if owner and owner.latitud and owner.longitud:
            center_lat = float(owner.latitud)
            center_lng = float(owner.longitud)

    features = generate_grid_polygons(center_lat, center_lng)

    geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    return jsonify(geojson)
