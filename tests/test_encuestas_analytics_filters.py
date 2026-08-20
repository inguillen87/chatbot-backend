from flask import Flask
from types import SimpleNamespace

from routes import encuestas_analytics as analytics_routes


def test_parse_filtros_includes_bbox_and_lists():
    app = Flask(__name__)
    with app.test_request_context('/x?bbox=-68,-33,-67,-32&canal=web&genero=femenino,masculino'):
        filtros = analytics_routes._parse_filtros()

    assert filtros['bbox'] == '-68,-33,-67,-32'
    assert filtros['canal'] == 'web'
    assert filtros['genero'] == ['femenino', 'masculino']


def test_employee_cannot_opt_into_synthetic_analytics_with_query_parameters():
    app = Flask(__name__)
    employee = SimpleNamespace(rol="empleado")
    with app.test_request_context('/x?data_mode=mixed&include_demo=true'):
        filtros = analytics_routes._parse_filtros(employee)

    assert filtros.get("data_mode") is None
    assert filtros.get("include_demo") is None
    assert filtros["exclude_demo"] == "true"
