import json
import random
from datetime import datetime, timedelta
from typing import Optional

import pytest

from extensions import db
from models import (
    AnalyticsEventV2,
    MunicipioTicket,
    PymePedido,
    PymeTicket,
    TenantProfile,
    TicketComentario,
    TicketSatisfaccion,
    User,
    EncComentario,
    EncEncuesta,
    EncRespuesta,
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


def _ensure_municipio_tenant(owner_id: int) -> tuple[User, TenantProfile]:
    owner = _ensure_user(owner_id, 'municipio')
    tenant = TenantProfile.query.filter_by(municipio_id=owner.id).one_or_none()
    if tenant is None:
        tenant = TenantProfile(
            slug=f'analytics-municipio-{owner.id}',
            nombre=f'Municipio Analytics {owner.id}',
            tipo='municipio',
            municipio_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
    return owner, tenant


def _analytics_profile_id(owner_id: int) -> int:
    return int(_ensure_municipio_tenant(owner_id)[1].id)


def _ensure_pyme_tenant(owner_id: int) -> tuple[User, TenantProfile]:
    owner = _ensure_user(owner_id, 'pyme')
    tenant = TenantProfile.query.filter_by(pyme_id=owner.id).one_or_none()
    if tenant is None:
        tenant = TenantProfile(
            slug=f'analytics-pyme-{owner.id}',
            nombre=f'PyME Analytics {owner.id}',
            tipo='pyme',
            pyme_id=owner.id,
        )
        db.session.add(tenant)
        db.session.flush()
    return owner, tenant


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
    admin, tenant = _ensure_municipio_tenant(tenant_id)
    ticket = MunicipioTicket(
        municipio_id=admin.id,
        tenant_id=tenant.id,
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
    admin, tenant = _ensure_pyme_tenant(tenant_id)
    ticket = PymeTicket(
        user_id=admin.id,
        tenant_id=tenant.id,
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
        pyme_id=admin.id,
        tenant_id=tenant.id,
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
    assert data['meta']['map']['provider_aliases']['maptiler'] == 'maplibre'
    assert 'available_providers' in data['meta']['map']
    assert data['render_contract']['module'] == 'heatmap'
    assert data['render_contract']['state'] in {'ready', 'empty'}
    assert response.headers.get('X-Request-Id')
    assert 'analytics_geo_heatmap' in (response.headers.get('Server-Timing') or '')


def test_geo_points_contract_headers(client):
    tenant_id = 22
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/geo/points',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'limit': 100},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data['points']
    assert data['render_contract']['module'] == 'points'
    assert data['render_contract']['state'] in {'ready', 'empty'}
    assert data['meta']['map']['fallback_provider'] == 'maplibre'
    assert response.headers.get('X-Request-Id')
    assert 'analytics_geo_points' in (response.headers.get('Server-Timing') or '')


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
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert templates.status_code == 200
    cohorts = client.get(
        '/analytics/cohorts',
        query_string={'tenant_id': tenant_id, 'scope': 'pyme'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert cohorts.status_code == 200


def test_operations_overview(client):
    tenant_id = 4
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/analytics/operations',
        query_string={'tenant_id': tenant_id, 'scope': 'operaciones'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['totals']['tickets'] >= 1
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
    assert response.status_code == 202
    assert response.get_json().get('reason') == 'tenant_unresolved'

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
    profile_id = _analytics_profile_id(tenant_id)
    response = client.post(
        '/analytics/event',
        json={
            'tenant_profile_id': profile_id,
            'owner_tenant_id': tenant_id,
            'tenant_type': 'municipio',
            'event_name': 'checkout_started',
            'payload': {'step': 'shipping'},
            'channel': 'web_widget',
            'session_id': 'sess_123',
        },
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 202
    saved = AnalyticsEventV2.query.filter_by(tenant_id=profile_id, event_name='checkout_started').first()
    assert saved is not None
    assert saved.metadata_payload.get('step') == 'shipping'

    forbidden = client.post(
        '/analytics/event',
        json={
            'tenant_profile_id': profile_id,
            'owner_tenant_id': tenant_id,
            'tenant_type': 'municipio',
            'event_name': 'page_view',
        },
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '999'},
    )
    assert forbidden.status_code == 202
    assert forbidden.get_json().get('reason') == 'access_denied'


def test_admin_analytics_overview_and_exports_are_tenant_scoped(client):
    tenant_id = 10
    ticket = _create_municipio_ticket(tenant_id)
    db.session.add(
        AnalyticsEventV2(
            tenant_id=ticket.tenant_id,
            event_name='ticket_created',
            tenant_type='municipio',
            channel='whatsapp',
            ts=datetime.utcnow(),
            metadata_payload={
                'categoria': 'seguridad',
                'barrio': 'centro',
                'distrito': 'norte',
            },
        )
    )
    db.session.commit()

    overview = client.get(
        '/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert overview.status_code == 200
    payload = overview.get_json()
    assert 'totals' in payload
    assert 'total_interactions' in (payload.get('totals') or {})

    csv_export = client.get(
        '/admin/analytics/export.csv',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert csv_export.status_code == 200
    assert csv_export.mimetype == 'text/csv'

    pdf_export = client.get(
        '/admin/analytics/export.pdf',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert pdf_export.status_code == 200
    assert pdf_export.mimetype == 'application/pdf'
    pdf_text = pdf_export.data.decode('latin-1', errors='ignore')
    assert 'Reporte de analytics' in pdf_text
    assert 'Segmentacion principal' in pdf_text
    assert r'seguridad \(1\)' in pdf_text
    assert 'Hotspots' in pdf_text
    xref_index = pdf_export.data.find(b"xref\n")
    assert xref_index > 0
    startxref_marker = b"startxref\n"
    marker_index = pdf_export.data.rfind(startxref_marker)
    assert marker_index > 0
    startxref_value = pdf_export.data[marker_index + len(startxref_marker):].split(b"\n", 1)[0]
    assert int(startxref_value) == xref_index

    forbidden = client.get(
        '/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': '999'},
    )
    assert forbidden.status_code == 403


def test_admin_analytics_heatmap_returns_temporal_matrix(client):
    tenant_id = 11
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='page_view',
            tenant_type='pyme',
            ts=now,
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'tz': 'America/Argentina/Cordoba'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data['tz'] == 'America/Argentina/Cordoba'
    assert isinstance(data['temporal'], list)
    assert 'segments' in data
    assert 'categoria' in data['segments']
    assert 'rango_edad' in data['segments']
    assert 'period_comparison' in data
    assert 'hotspots' in data


def test_admin_analytics_heatmap_rejects_non_numeric_tenant_id(client):
    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': 'abc', 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': 'abc'},
    )
    assert response.status_code == 400


def test_admin_analytics_whatsapp_funnel_reads_profile_scoped_events(client):
    owner_id = 217
    profile_id = _analytics_profile_id(owner_id)
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='whatsapp_portal_menu_opened',
            tenant_type='municipio',
            channel='whatsapp',
            session_id='wa-profile-scoped-session',
            ts=datetime.utcnow(),
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/whatsapp-funnel',
        query_string={'tenant_id': owner_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(owner_id)},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data.get('totals', {}).get('events') == 1
    assert (data.get('stages') or [])[0].get('event_name') == 'whatsapp_portal_menu_opened'
    assert (data.get('stages') or [])[0].get('unique_sessions') == 1




def test_admin_analytics_heatmap_segments_from_event_metadata(client):
    tenant_id = 211
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='ticket_created',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            metadata_payload={
                'categoria': 'alumbrado',
                'barrio': 'centro',
                'distrito': 'norte',
                'sexo': 'f',
                'edad': 31,
            },
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    segments = data.get('segments') or {}
    assert (segments.get('categoria') or [])[0]['label'] == 'alumbrado'
    assert (segments.get('barrio') or [])[0]['label'] == 'centro'
    assert (segments.get('distrito') or [])[0]['label'] == 'norte'
    assert (segments.get('sexo') or [])[0]['label'] == 'f'



def test_admin_analytics_heatmap_applies_segment_filters(client):
    tenant_id = 212
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='ticket_created',
            tenant_type='municipio',
            channel='web_widget',
            ts=now - timedelta(minutes=10),
            metadata_payload={'categoria': 'alumbrado', 'barrio': 'centro', 'distrito': 'norte', 'sexo': 'f', 'edad': 31},
        )
    )
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='ticket_created',
            tenant_type='municipio',
            channel='whatsapp',
            ts=now,
            metadata_payload={'categoria': 'limpieza', 'barrio': 'sur', 'distrito': 'sur', 'sexo': 'm', 'edad': 52},
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'categoria': 'alumbrado', 'sexo': 'f'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    filters_applied = data.get('segments_filters_applied') or {}
    assert filters_applied.get('categoria') == ['alumbrado']
    assert filters_applied.get('sexo') == ['f']
    categorias = data.get('segments', {}).get('categoria') or []
    assert categorias and categorias[0]['label'] == 'alumbrado'


def test_admin_analytics_heatmap_includes_maplibre_layers_with_category_colors(client):
    tenant_id = 213
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='encuesta_voto',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            metadata_payload={
                'categoria': 'seguridad',
                'lat': -34.6037,
                'lng': -58.3816,
                'votos': 8,
            },
        )
    )
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='encuesta_voto',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            metadata_payload={
                'categoria': 'transito',
                'lat': -34.6118,
                'lng': -58.4173,
                'votos': 3,
            },
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data.get('contract_version') == 'analytics.heatmap.v1'
    assert data.get('request_id')
    assert (data.get('points') or [])[0]['categoria'] == 'seguridad'
    geo_layers = data.get('geo_layers') or {}
    assert geo_layers.get('provider') == 'maplibre'
    assert geo_layers.get('engine') == 'maplibre-gl-js'
    assert geo_layers.get('contract_version') == '2026.04-maplibre-v1'
    assert geo_layers.get('source', {}).get('type') == 'FeatureCollection'
    assert geo_layers.get('source_meta', {}).get('limit') == 2000
    assert geo_layers.get('telemetry', {}).get('events')
    assert geo_layers.get('layers', {}).get('heatmap', {}).get('type') == 'heatmap'
    categories = geo_layers.get('categories') or []
    assert categories
    assert categories[0].get('categoria') == 'seguridad'
    assert categories[0].get('color')
    assert categories[0].get('total_weight') == 8
    assert categories[0].get('points')
    assert geo_layers.get('legend', {}).get('max_weight') == 8


def test_admin_analytics_heatmap_honors_maplibre_style_url_from_config(client):
    tenant_id = 216
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='encuesta_voto',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            metadata_payload={
                'categoria': 'seguridad',
                'lat': -34.6037,
                'lng': -58.3816,
                'votos': 4,
            },
        )
    )
    db.session.commit()

    client.application.config['MAPLIBRE_STYLE_URL'] = 'https://maps.example.com/style.json'
    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'geo_limit': 100, 'bbox': '-59,-35,-58,-34'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    geo_layers = (response.get_json() or {}).get('geo_layers') or {}
    assert geo_layers.get('style_url') == 'https://maps.example.com/style.json'
    assert geo_layers.get('source_meta', {}).get('limit') == 100
    assert geo_layers.get('source_meta', {}).get('bbox') == [-59.0, -35.0, -58.0, -34.0]


def test_admin_analytics_heatmap_uses_persisted_lat_lng_for_geo_layers(client):
    tenant_id = 215
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='ticket_created',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            lat=-34.6037,
            lng=-58.3816,
            metadata_payload={
                'categoria': 'seguridad',
                'barrio': 'centro',
                'distrito': 'norte',
            },
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    categories = ((data.get('geo_layers') or {}).get('categories') or [])
    assert categories
    first_points = categories[0].get('points') or []
    assert first_points
    assert first_points[0]['lat'] == -34.6037
    assert first_points[0]['lng'] == -58.3816



def test_admin_analytics_heatmap_supports_genero_alias_and_age_bucket(client):
    tenant_id = 214
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='encuesta_voto',
            tenant_type='municipio',
            channel='web_widget',
            ts=now,
            metadata_payload={
                'categoria': 'salud',
                'barrio': 'centro',
                'distrito': 'norte',
                'genero': 'f',
                'edad': 22,
                'lat': -34.60,
                'lng': -58.39,
                'votos': 5,
            },
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'genero': 'f'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    filters_applied = data.get('segments_filters_applied') or {}
    assert filters_applied.get('sexo') == ['f']

    segments = data.get('segments') or {}
    assert (segments.get('sexo') or [])[0]['label'] == 'f'
    assert (segments.get('rango_edad') or [])[0]['label'] == '18-24'

def test_api_alias_admin_analytics_overview_and_heatmap(client):
    tenant_id = 12
    ticket = _create_municipio_ticket(tenant_id)
    db.session.add(
        AnalyticsEventV2(
            tenant_id=ticket.tenant_id,
            event_name='alias_view',
            tenant_type='municipio',
            ts=datetime.utcnow(),
        )
    )
    db.session.commit()

    overview = client.get(
        '/api/admin/analytics/overview',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert overview.status_code == 200

    heatmap = client.get(
        '/api/admin/analytics/heatmap',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert heatmap.status_code == 200


def test_api_alias_admin_analytics_overview_accepts_tenant_slug(client):
    tenant_id = 21
    ticket = _create_municipio_ticket(tenant_id)
    db.session.flush()

    tenant = db.session.get(TenantProfile, ticket.tenant_id)
    tenant.slug = 'tenant-analytics-slug'
    db.session.commit()

    overview = client.get(
        '/api/admin/analytics/overview',
        query_string={'tenant_slug': 'tenant-analytics-slug', 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert overview.status_code == 200
    assert 'totals' in overview.get_json()




def test_admin_analytics_realtime_hub_includes_surveys_and_geo(client):
    tenant_id = 333
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='realtime_business_action_executed',
            tenant_type='municipio',
            channel='realtime_voice',
            ts=now,
            lat=-34.61,
            lng=-58.38,
            metadata_payload={
                'categoria': 'alumbrado',
                'barrio': 'centro',
                'distrito': 'norte',
                'sentiment': 'positive',
                'comment': 'Reporte realtime con ubicacion',
            },
        )
    )
    encuesta = EncEncuesta(
        tenant_id=profile_id,
        slug=f"encuesta-{tenant_id}",
        titulo='Sondeo Express',
        estado='publicada',
    )
    db.session.add(encuesta)
    db.session.flush()

    db.session.add(
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=profile_id,
            huella_unica=f"fingerprint-{tenant_id}",
            canal='web',
        )
    )
    db.session.add(
        EncRespuesta(
            encuesta_id=encuesta.id,
            tenant_id=profile_id,
            response_origin='synthetic_demo',
            huella_unica=f"trusted-synthetic-{tenant_id}",
            canal='web',
            metadata_payload={
                'is_demo_seed': True,
                'demo_seed_contract_version': 'surveys.demo_seeding.v1',
                'demo_batch_id': f'seed-{encuesta.id}-1720000000',
            },
        )
    )
    db.session.add(
        EncComentario(
            encuesta_id=encuesta.id,
            texto='Muy buena atención',
            anon_id='anon-test',
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/realtime-hub',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'window_minutes': 60},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data.get('contract_version') == 'analytics.realtime_hub.v1'
    assert data.get('request_id')
    assert data.get('totals', {}).get('events', 0) >= 1
    assert data.get('totals', {}).get('survey_responses') == 1
    assert data.get('response_provenance', {}).get('synthetic_responses_excluded') == 1
    assert data.get('totals', {}).get('survey_comments', 0) >= 1
    survey_ops = data.get('survey_operations') or {}
    assert survey_ops.get('contract_version') == 'analytics.survey_operations.v1'
    assert survey_ops.get('status') == 'live'
    assert survey_ops.get('responses') >= 1
    assert survey_ops.get('comments') >= 1
    assert survey_ops.get('engagement') >= 2
    assert (survey_ops.get('recommended_actions') or [])[0].get('id') == 'moderate_comments'
    assert (data.get('top_channels') or [])[0]['channel'] == 'realtime_voice'
    assert (data.get('top_events') or [])[0]['event'] == 'realtime_business_action_executed'
    assert data.get('comments')
    assert data.get('hotspots') == [{'label': 'centro', 'count': 1}]
    assert (data.get('geo_points') or [])[0]['channel'] == 'realtime_voice'
    geo_layers = data.get('geo_layers') or {}
    assert geo_layers.get('provider') == 'maplibre'
    assert geo_layers.get('engine') == 'maplibre-gl-js'
    assert geo_layers.get('contract_version') == '2026.04-maplibre-v1'
    assert geo_layers.get('source', {}).get('features')
    assert geo_layers.get('source_options', {}).get('cluster') is True
    assert geo_layers.get('layers', {}).get('heatmap', {}).get('id') == 'events-heat'
    assert geo_layers.get('layers', {}).get('clusters', {}).get('id') == 'events-clusters'
    assert geo_layers.get('layers', {}).get('points', {}).get('id') == 'events-points'
    assert geo_layers.get('telemetry', {}).get('event_endpoint') == '/api/analytics/event'
    assert (data.get('segments') or {}).get('categoria')
    labels = (data.get('ui') or {}).get('labels', {})
    assert labels.get('tabs_realtime_hub') == 'Realtime Hub'
    assert labels.get('survey_ops_title') == 'Encuestas y votaciones en vivo'
    assert labels.get('sections_map') == 'Mapa en tiempo real'
    assert labels.get('sections_segments') == 'Segmentos'
    assert labels.get('empty_map')
    assert isinstance((data.get('geo') or {}).get('points'), list)
    assert isinstance((data.get('recommendations') or []), list)


def test_admin_analytics_realtime_hub_applies_segment_filters_to_geo_layers(client):
    tenant_id = 339
    profile_id = _analytics_profile_id(tenant_id)
    now = datetime.utcnow()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='crear_reclamo',
            tenant_type='municipio',
            channel='voice',
            ts=now,
            lat=-34.61,
            lng=-58.38,
            metadata_payload={
                'categoria': 'seguridad',
                'barrio': 'centro',
                'distrito': 'norte',
                'sexo': 'f',
                'edad': 31,
                'votos': 8,
            },
        )
    )
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='crear_reclamo',
            tenant_type='municipio',
            channel='web',
            ts=now,
            lat=-34.7,
            lng=-58.5,
            metadata_payload={
                'categoria': 'transito',
                'barrio': 'sur',
                'distrito': 'sur',
                'sexo': 'm',
                'edad': 42,
                'votos': 3,
            },
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/realtime-hub',
        query_string={
            'tenant_id': tenant_id,
            'scope': 'municipio',
            'window_minutes': 60,
            'categoria': 'seguridad',
            'canal': 'voice',
            'sexo': 'f',
            'rango_edad': '25-34',
            'geo_limit': 125,
            'bbox': '-59,-35,-58,-34',
        },
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data.get('segments_filters_applied') == {
        'canal': ['voice'],
        'categoria': ['seguridad'],
        'rango_edad': ['25-34'],
        'sexo': ['f'],
    }
    assert (data.get('segments') or {}).get('categoria') == [{'label': 'seguridad', 'count': 1}]
    geo_layers = data.get('geo_layers') or {}
    assert geo_layers.get('source_meta', {}).get('limit') == 125
    assert geo_layers.get('source_meta', {}).get('bbox') == [-59.0, -35.0, -58.0, -34.0]
    features = geo_layers.get('source', {}).get('features') or []
    assert len(features) == 1
    assert features[0]['properties']['categoria'] == 'seguridad'
    assert (geo_layers.get('categories') or [])[0]['total_weight'] == 8


def test_api_alias_admin_analytics_realtime_hub_available(client):
    tenant_id = 334
    profile_id = _analytics_profile_id(tenant_id)
    db.session.add(
        AnalyticsEventV2(
            tenant_id=profile_id,
            event_name='cluster_click',
            tenant_type='municipio',
            channel='web',
            ts=datetime.utcnow(),
            metadata_payload={'categoria': 'seguridad'},
        )
    )
    db.session.commit()

    response = client.get(
        '/api/admin/analytics/realtime-hub',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    assert response.get_json().get('contract_version') == 'analytics.realtime_hub.v1'

def test_admin_analytics_overview_accepts_debug_tenant_without_query_tenant(client):
    tenant_id = 31
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/overview',
        query_string={'scope': 'municipio'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
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
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
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
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert first.status_code == 200
    etag = first.headers.get('ETag')
    assert etag

    second = client.get(
        '/admin/analytics/dashboard',
        query_string={'scope': 'municipio'},
        headers={
            'X-Debug-Role': 'admin',
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
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
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
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
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
            'X-Debug-Role': 'admin',
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
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload.get('event_name') == 'page_view'
    assert payload.get('tenant_id') == tenant_id


def test_api_alias_analytics_event_maps_to_ingestor(client):
    tenant_id = 35
    profile_id = _analytics_profile_id(tenant_id)

    response = client.post(
        '/api/analytics/event',
        json={
            'tenant_profile_id': profile_id,
            'owner_tenant_id': tenant_id,
            'tenant_type': 'municipio',
            'event_name': 'dashboard_open',
        },
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload.get('ok') is True
    assert payload.get('request_id')
    assert response.headers.get('X-Request-Id') == payload.get('request_id')
    assert AnalyticsEventV2.query.filter_by(tenant_id=profile_id, event_name='dashboard_open').first() is not None


def test_event_ingest_does_not_hide_unexpected_access_errors(client, monkeypatch):
    tenant_id = 8
    profile_id = _analytics_profile_id(tenant_id)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("routes.analytics.require_access", _boom)

    with pytest.raises(RuntimeError):
        client.post(
            '/analytics/event',
            json={
                'tenant_profile_id': profile_id,
                'owner_tenant_id': tenant_id,
                'tenant_type': 'municipio',
                'event_name': 'page_view',
            },
            headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': str(tenant_id)},
        )


def test_api_alias_analytics_event_honors_feature_gate(client, monkeypatch):
    monkeypatch.setitem(client.application.config, "ANALYTICS_ENABLED", False)

    response = client.post(
        '/api/analytics/event',
        json={'tenant_id': 35, 'event_name': 'dashboard_open'},
        headers={'X-Debug-Role': 'operador', 'X-Debug-Tenant': '35'},
    )

    assert response.status_code == 404


def test_admin_analytics_realtime_hub_accepts_invalid_window_minutes(client):
    tenant_id = 337
    _create_municipio_ticket(tenant_id)
    db.session.commit()

    response = client.get(
        '/admin/analytics/realtime-hub',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'window_minutes': 'abc'},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data.get('window_minutes') == 30


def test_admin_analytics_realtime_hub_counts_live_chat_comments_by_tenant_ticket(client):
    tenant_id = 338
    ticket = _create_municipio_ticket(tenant_id)
    db.session.flush()
    db.session.add(
        TicketComentario(
            municipio_ticket_id=ticket.id,
            comentario='Seguimiento realtime',
            user_id=999999,
            es_admin=False,
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/realtime-hub',
        query_string={'tenant_id': tenant_id, 'scope': 'municipio', 'window_minutes': 60},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(tenant_id)},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data.get('totals', {}).get('live_chat_comments') >= 1


def test_admin_analytics_realtime_hub_never_treats_owner_as_foreign_profile_pk(client):
    owner_id = 340
    foreign_owner_id = 341
    _ensure_user(owner_id, 'municipio')
    foreign_owner = _ensure_user(foreign_owner_id, 'municipio')
    foreign_tenant = TenantProfile(
        id=owner_id,
        slug='analytics-foreign-profile-collision',
        nombre='Foreign profile collision',
        tipo='municipio',
        municipio_id=foreign_owner.id,
    )
    db.session.add(foreign_tenant)
    db.session.flush()
    foreign_ticket = _create_municipio_ticket(foreign_owner_id)
    legacy_unscoped_pyme_ticket = _create_pyme_ticket(owner_id, fecha=datetime.utcnow())
    legacy_unscoped_pyme_ticket.tenant_id = None
    db.session.add(
        AnalyticsEventV2(
            tenant_id=foreign_tenant.id,
            event_name='foreign_tenant_event',
            tenant_type='municipio',
            channel='web',
            ts=datetime.utcnow(),
        )
    )
    db.session.commit()

    response = client.get(
        '/admin/analytics/realtime-hub',
        query_string={'tenant_id': owner_id, 'scope': 'municipio', 'window_minutes': 60},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(owner_id)},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data.get('totals', {}).get('events') == 0
    assert data.get('totals', {}).get('live_chat_comments') == 0
    assert foreign_ticket.tenant_id == foreign_tenant.id
    assert legacy_unscoped_pyme_ticket.tenant_id is None


def test_admin_analytics_realtime_hub_requires_exact_profile_for_ambiguous_owner(client):
    owner_id = 342
    owner = _ensure_user(owner_id, 'municipio')
    tenants = [
        TenantProfile(
            slug=f'analytics-ambiguous-{suffix}',
            nombre=f'Ambiguous tenant {suffix}',
            tipo='municipio',
            municipio_id=owner.id,
        )
        for suffix in ('one', 'two')
    ]
    db.session.add_all(tenants)
    db.session.flush()
    db.session.add(
        AnalyticsEventV2(
            tenant_id=tenants[0].id,
            event_name='selected_tenant_event',
            tenant_type='municipio',
            channel='web',
            ts=datetime.utcnow(),
        )
    )
    db.session.commit()

    raw_owner = client.get(
        '/admin/analytics/realtime-hub',
        query_string={'tenant_id': owner_id, 'scope': 'municipio', 'window_minutes': 60},
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(owner_id)},
    )
    exact_profile = client.get(
        '/admin/analytics/realtime-hub',
        query_string={
            'tenant_id': owner_id,
            'tenant_profile_id': tenants[0].id,
            'scope': 'municipio',
            'window_minutes': 60,
        },
        headers={'X-Debug-Role': 'admin', 'X-Debug-Tenant': str(owner_id)},
    )

    assert raw_owner.status_code == 200
    assert raw_owner.get_json().get('totals', {}).get('events') == 0
    assert exact_profile.status_code == 200
    assert exact_profile.get_json().get('totals', {}).get('events') == 1
