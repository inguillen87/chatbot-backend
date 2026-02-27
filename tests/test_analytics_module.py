import json
import random
from datetime import datetime, timedelta
from typing import Optional

from extensions import db
from models import (
    AnalyticsEventV2,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TicketComentario,
    TicketSatisfaccion,
    User,
)


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
    assert 'meta' in data
    assert 'map' in data['meta']


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


def test_event_ingest_requires_tenant_and_event_name(client):
    response = client.post(
        '/analytics/event',
        json={'event_name': 'page_view'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '8'},
    )
    assert response.status_code == 400

    response = client.post(
        '/analytics/event',
        json={'tenant_id': 8},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '8'},
    )
    assert response.status_code == 202
    payload = response.get_json()
    assert payload.get('event_name') == 'frontend_analytics_event'


def test_event_ingest_is_tenant_scoped(client):
    tenant_id = 9
    response = client.post(
        '/analytics/event',
        json={
            'tenant_id': tenant_id,
            'event_name': 'checkout_started',
            'payload': {'step': 'shipping'},
            'channel': 'web_widget',
            'session_id': 'sess_123',
        },
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 202
    saved = AnalyticsEventV2.query.filter_by(tenant_id=tenant_id, event_name='checkout_started').first()
    assert saved is not None
    assert saved.metadata_payload.get('step') == 'shipping'

    forbidden = client.post(
        '/analytics/event',
        json={'tenant_id': tenant_id, 'event_name': 'page_view'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '999'},
    )
    assert forbidden.status_code == 403


def test_admin_analytics_overview_and_exports_are_tenant_scoped(client):
    tenant_id = 10
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    overview = client.get(
        '/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert overview.status_code == 200
    payload = overview.get_json()
    assert 'totals' in payload
    assert 'total_interactions' in (payload.get('totals') or {})

    csv_export = client.get(
        '/admin/analytics/export.csv',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert csv_export.status_code == 200
    assert csv_export.mimetype == 'text/csv'

    pdf_export = client.get(
        '/admin/analytics/export.pdf',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert pdf_export.status_code == 200
    assert pdf_export.mimetype == 'application/pdf'

    forbidden = client.get(
        '/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '999'},
    )
    assert forbidden.status_code == 403


def test_admin_analytics_heatmap_returns_temporal_matrix(client):
    tenant_id = 11
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=tenant_id,
            event_name='page_view',
            tenant_type='pyme',
            ts=now,
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'tz': 'America/Argentina/Cordoba'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['tz'] == 'America/Argentina/Cordoba'
    assert isinstance(data['temporal'], list)


def test_admin_analytics_heatmap_rejects_non_numeric_tenant_id(client):
    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': 'abc', 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': 'abc'},
    )
    assert response.status_code == 400


def test_api_alias_admin_analytics_overview_and_heatmap(client):
    tenant_id = 12
    _create_municipio_ticket(tenant_id)
    db.session.add(
        AnalyticsEventV2(
            tenant_id=tenant_id,
            event_name='alias_view',
            tenant_type='municipio',
            ts=datetime.utcnow(),
        )
    )
    db.session.commit()

    overview = client.get(
        '/api/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert overview.status_code == 200

    heatmap = client.get(
        '/api/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert heatmap.status_code == 200


def test_api_alias_admin_analytics_overview_accepts_tenant_slug(client):
    tenant_id = 21
    _create_municipio_ticket(tenant_id)
    db.session.flush()

    from models import TenantProfile

    tenant = TenantProfile(
        slug='tenant-analytics-slug',
        nombre='Tenant Analytics Slug',
        tipo='municipio',
        municipio_id=tenant_id,
    )
    db.session.add(tenant)
    db.session.commit()

    overview = client.get(
        '/api/admin/analytics/overview',
        query_string={'tenant_slug': 'tenant-analytics-slug', 'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant.id)},
    )
    assert overview.status_code == 200
    assert 'totals' in overview.get_json()


def test_admin_analytics_overview_accepts_debug_tenant_without_query_tenant(client):
    tenant_id = 31
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/overview',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload.get('totals', {}).get('total_interactions') is not None


def test_admin_analytics_dashboard_returns_unified_sections(client):
    tenant_id = 32
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/dashboard',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload.get('tenant_id') == str(tenant_id)
    sections = payload.get('sections') or {}
    assert {'general', 'municipio', 'ventas', 'mapas'}.issubset(set(sections.keys()))
    nav = payload.get('navigation') or {}
    assert isinstance(nav.get('primary'), list) and nav.get('primary')
    assert (nav.get('encuestas') or {}).get('seed_demo_endpoint_template')


def test_admin_analytics_dashboard_etag_returns_304(client):
    tenant_id = 33
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    first = client.get(
        '/admin/analytics/dashboard',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert first.status_code == 200
    etag = first.headers.get('ETag')
    assert etag

    second = client.get(
        '/admin/analytics/dashboard',
        query_string={'scope': 'municipio'},
        headers={
            'X-Debug-Role': 'operador',
            'X-Debug-Tenant': str(tenant_id),
            'If-None-Match': etag,
        },
    )
    assert second.status_code == 304


def test_api_alias_admin_analytics_hub_available(client):
    tenant_id = 34
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/api/admin/analytics/hub',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert (payload.get('sections') or {}).get('general') is not None


def test_admin_analytics_hub_includes_meta_and_contract_headers(client):
    tenant_id = 35
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/hub',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    assert response.headers.get('X-Analytics-Request-Id')
    assert response.headers.get('X-Analytics-Contract-Version')

    payload = response.get_json()
    meta = payload.get('meta') or {}
    assert meta.get('contract_version')
    assert meta.get('generated_at')
    assert meta.get('request_id')
    cache = meta.get('cache') or {}
    assert isinstance(cache.get('hit'), bool)


def test_admin_analytics_hub_honors_custom_request_id(client):
    tenant_id = 36
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/hub',
        query_string={'scope': 'municipio'},
        headers={
            'X-Debug-Role': 'operador',
            'X-Debug-Tenant': str(tenant_id),
            'X-Request-Id': 'req-demo-123',
        },
    )
    assert response.status_code == 200
    assert response.headers.get('X-Analytics-Request-Id') == 'req-demo-123'
    payload = response.get_json()
    assert (payload.get('meta') or {}).get('request_id') == 'req-demo-123'


def test_event_ingest_accepts_query_tenant_slug(client):
    tenant_id = 34
    _ensure_user(tenant_id, 'municipio')

    from models import TenantProfile

    tenant = TenantProfile(
        slug='junin-1',
        nombre='Junín',
        tipo='municipio',
        municipio_id=tenant_id,
    )
    db.session.add(tenant)
    db.session.commit()

    response = client.post(
        '/analytics/event',
        query_string={'tenant_slug': 'junin-1', 'tenant': 'junin-1'},
        json={'event_name': 'page_view'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant.id)},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload.get('event_name') == 'page_view'
    assert payload.get('tenant_id') == tenant.id


def test_api_alias_analytics_event_maps_to_ingestor(client):
    tenant_id = 35
    _ensure_user(tenant_id, 'municipio')

    response = client.post(
        '/api/analytics/event',
        json={'tenant_id': tenant_id, 'event_name': 'dashboard_open'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )

    assert response.status_code == 202
    assert AnalyticsEventV2.query.filter_by(tenant_id=tenant_id, event_name='dashboard_open').first() is not None
