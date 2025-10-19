import json
import random
from datetime import datetime, timedelta
from typing import Optional

from extensions import db
from models import MunicipioTicket, PymePedido, PymeTicket, TicketComentario, TicketSatisfaccion, User


def _ensure_user(tenant_id: int, scope: str) -> User:
    user = User.query.get(tenant_id)
    if user:
        return user
    email = f"tenant_{scope}_{tenant_id}@example.com"
    user = User(
        id=tenant_id,
        name=f"Tenant {tenant_id}",
        email=email,
        rol='admin',
        tipo_chat=scope,
        municipio_id=tenant_id if scope == 'municipio' else None,
        pyme_id=tenant_id if scope == 'pyme' else None,
    )
    user.set_password('test')
    db.session.add(user)
    db.session.flush()
    return user


def random_ticket_number() -> int:
    return random.randint(1_000_000, 9_999_999)


def _create_municipio_ticket(
    tenant_id: int,
    *,
    fecha: Optional[datetime] = None,
    categoria: str = "iluminación",
    canal: str = "whatsapp",
    estado: str = "en_progreso",
    distrito: str = "Centro",
    latitud: float = -34.6,
    longitud: float = -58.4,
) -> MunicipioTicket:
    admin = _ensure_user(tenant_id, 'municipio')
    ticket = MunicipioTicket(
        municipio_id=tenant_id,
        pregunta='bache en la calle',
        categoria=categoria,
        canal_ingreso=canal,
        estado=estado,
        fecha=fecha or (datetime.utcnow() - timedelta(hours=6)),
        latitud=latitud,
        longitud=longitud,
        distrito=distrito,
    )
    db.session.add(ticket)
    db.session.flush()
    comment = TicketComentario(
        municipio_ticket_id=ticket.id,
        comentario='Derivamos al área de obras',
        fecha=(fecha or ticket.fecha) + timedelta(minutes=30),
        es_admin=True,
        estado_ticket='en_progreso',
        user_id=admin.id,
    )
    db.session.add(comment)
    survey = TicketSatisfaccion(
        ticket_id=ticket.id,
        tipo='municipio',
        puntuacion=9,
    )
    db.session.add(survey)
    return ticket


def _create_pyme_ticket(
    tenant_id: int,
    *,
    fecha: Optional[datetime] = None,
    categoria: str = 'Delivery',
    estado: str = 'en_progreso',
    latitud: float = -34.65,
    longitud: float = -58.45,
) -> PymeTicket:
    admin = _ensure_user(tenant_id, 'pyme')
    ticket = PymeTicket(
        user_id=tenant_id,
        pregunta='Necesito reposición de stock',
        categoria=categoria,
        estado=estado,
        fecha=fecha or (datetime.utcnow() - timedelta(days=1)),
        latitud=latitud,
        longitud=longitud,
        nro_ticket=random_ticket_number(),
    )
    db.session.add(ticket)
    db.session.flush()
    comment = TicketComentario(
        pyme_ticket_id=ticket.id,
        comentario='Listo para enviar',
        fecha=(fecha or ticket.fecha) + timedelta(minutes=20),
        es_admin=True,
        estado_ticket='en_progreso',
        user_id=admin.id,
    )
    db.session.add(comment)
    pedido = PymePedido(
        pyme_id=tenant_id,
        asunto='Pedido mayorista',
        detalles=json.dumps({'nombre': 'Producto X', 'qty': 2, 'precio': 3500}),
        monto_total=7000,
        nombre_cliente='Test',
        email_cliente='test@example.com',
        telefono_cliente='+54110000000',
        latitud=-34.65,
        longitud=-58.45,
    )
    db.session.add(pedido)
    survey = TicketSatisfaccion(
        ticket_id=ticket.id,
        tipo='pyme',
        puntuacion=4,
    )
    db.session.add(survey)
    return ticket


def test_municipio_summary(client):
    tenant_id = 1
    ticket = _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/summary',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['totals']['tickets'] == 1
    assert data['totals']['nps'] is not None
    assert 'tta' in data['sla']
    assert data['totals']['tickets_cerrados'] is not None
    assert 'cierre_pct' in data['totals']
    assert 'tickets_variacion_pct' in data['totals']


def test_geo_heatmap(client):
    tenant_id = 2
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/geo/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['cells']


def test_pyme_endpoints(client):
    tenant_id = 3
    _create_pyme_ticket(tenant_id)
    db.session.commit()

    summary = client.get(
        '/analytics/summary',
        query_string={'tenant_id': tenant_id, 'scope': 'pyme'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert summary.status_code == 200
    data = summary.get_json()
    assert 'pedidos_variacion_pct' in data['totals']
    assert 'ttr_promedio_min' in data['totals']
    templates = client.get(
        '/analytics/whatsapp/templates',
        query_string={'tenant_id': tenant_id, 'scope': 'pyme'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert templates.status_code == 200
    cohorts = client.get(
        '/analytics/cohorts',
        query_string={'tenant_id': tenant_id, 'scope': 'pyme'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert cohorts.status_code == 200


def test_operations_overview(client):
    tenant_id = 4
    _create_municipio_ticket(tenant_id)
    _create_pyme_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/operations',
        query_string={'tenant_id': tenant_id, 'scope': 'operaciones'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['totals']['tickets'] >= 2
    assert 'aging' in data['extras']
    assert 'tickets_cerrados' in data['totals']
    assert 'cierre_pct' in data['totals']
    assert data['extras']['agents']


def test_timeseries_grouping_and_filters(client):
    tenant_id = 5
    base = datetime.utcnow()
    _create_municipio_ticket(
        tenant_id,
        fecha=base - timedelta(days=1),
        categoria='iluminación',
        canal='whatsapp',
    )
    _create_municipio_ticket(
        tenant_id,
        fecha=base - timedelta(days=2),
        categoria='bacheo',
        canal='web',
    )
    db.session.commit()

    response = client.get(
        '/analytics/timeseries',
        query_string={
            'tenant_id': tenant_id,
            'scope': 'municipio',
            'group': 'categoria',
            'from': (base - timedelta(days=3)).strftime('%Y-%m-%d'),
            'to': base.strftime('%Y-%m-%d'),
            'canal': 'whatsapp',
        },
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    series = response.get_json()['series']
    assert series
    groups = {item['group'] for item in series}
    assert groups == {'iluminación'}
    assert all(item['value'] == 1 for item in series)


def test_breakdown_respects_date_and_category_filters(client):
    tenant_id = 6
    today = datetime.utcnow()
    _create_municipio_ticket(
        tenant_id,
        fecha=today - timedelta(days=30),
        categoria='iluminación',
    )
    _create_municipio_ticket(
        tenant_id,
        fecha=today - timedelta(days=1),
        categoria='bacheo',
    )
    db.session.commit()

    response = client.get(
        '/analytics/breakdown',
        query_string={
            'tenant_id': tenant_id,
            'scope': 'municipio',
            'dimension': 'categoria',
            'from': (today - timedelta(days=2)).strftime('%Y-%m-%d'),
            'categoria': 'bacheo',
        },
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()['breakdown']
    assert data == [{'label': 'bacheo', 'value': 1}]


def test_operations_requires_operator_role(client):
    tenant_id = 7
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/operations',
        query_string={'tenant_id': tenant_id, 'scope': 'operaciones'},
        headers={'X-Debug-Role': 'visor', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 403
