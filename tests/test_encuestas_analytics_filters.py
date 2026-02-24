from flask import Flask

from routes import encuestas_analytics as analytics_routes


def test_parse_filtros_includes_bbox_and_lists():
    app = Flask(__name__)
    with app.test_request_context('/x?bbox=-68,-33,-67,-32&canal=web&genero=femenino,masculino'):
        filtros = analytics_routes._parse_filtros()

    assert filtros['bbox'] == '-68,-33,-67,-32'
    assert filtros['canal'] == 'web'
    assert filtros['genero'] == ['femenino', 'masculino']
