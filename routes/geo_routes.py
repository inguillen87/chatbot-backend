from flask import Blueprint, jsonify, request

geo_bp = Blueprint('geo_bp', __name__, url_prefix='/api/geo')

@geo_bp.route('/polygons', methods=['GET'])
def get_polygons():
    """
    Returns GeoJSON polygons for neighborhoods/barrios.
    Mocked implementation for visualization demos (Chloropleth Maps).
    """
    city_id = request.args.get('city_id')

    # Mock data: A couple of polygons (squares) roughly in Buenos Aires or generic.
    # Coordinates in [lng, lat] format (GeoJSON standard).
    # Center approx: -34.6037, -58.3816 (Obelisco)

    # Polygon 1: Microcentro (High Density)
    poly1 = [
        [-58.3800, -34.6000],
        [-58.3700, -34.6000],
        [-58.3700, -34.6100],
        [-58.3800, -34.6100],
        [-58.3800, -34.6000]
    ]

    # Polygon 2: San Telmo (Medium Density)
    poly2 = [
        [-58.3800, -34.6100],
        [-58.3700, -34.6100],
        [-58.3700, -34.6200],
        [-58.3800, -34.6200],
        [-58.3800, -34.6100]
    ]

    # Polygon 3: Puerto Madero (Low Density)
    poly3 = [
        [-58.3700, -34.6000],
        [-58.3600, -34.6000],
        [-58.3600, -34.6200],
        [-58.3700, -34.6200],
        [-58.3700, -34.6000]
    ]

    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "name": "Zona Centro",
                    "density": 85,
                    "value": 1500, # e.g. metric for color scale
                    "fill": "#FF0000"
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [poly1]
                }
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "Zona Sur",
                    "density": 45,
                    "value": 800,
                    "fill": "#FFFF00"
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [poly2]
                }
            },
            {
                "type": "Feature",
                "properties": {
                    "name": "Zona Puerto",
                    "density": 20,
                    "value": 300,
                    "fill": "#00FF00"
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [poly3]
                }
            }
        ]
    }

    return jsonify(geojson)
