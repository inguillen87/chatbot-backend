"""Selected source handlers with real Flask/SQLite and isolated collaborators.

No app factory, provider, configuration, decorator or production DB is loaded.
The mapped fixture schema covers the fields queried by the actual history code.
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Union
import unittest

from flask import Flask, current_app, g, jsonify, make_response, request
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine, event, func, or_
from sqlalchemy.orm import Session, declarative_base

ROOT = Path(__file__).resolve().parents[1]
Base = declarative_base()


class TicketFields:
    id = Column(Integer, primary_key=True)
    tenant_id = Column(Integer)
    user_id = Column(Integer)
    anon_id = Column(String(80))
    nro_ticket = Column(String(36))
    consulta_pin = Column(String(6))
    fecha = Column(DateTime)
    estado = Column(String(30))


class MunicipalFixture(TicketFields, Base):
    __tablename__ = 'scope_municipal_fixture'


class BusinessFixture(TicketFields, Base):
    __tablename__ = 'scope_business_fixture'


class LegacyFixture(Base):
    __tablename__ = 'scope_legacy_fixture'
    id = Column(Integer, primary_key=True)
    session_id = Column(String(36))
    pyme_id = Column(Integer)
    user_id = Column(Integer)
    rubro = Column(String(100))
    pregunta = Column(Text)
    respuesta = Column(Text)
    timestamp = Column(DateTime)


class CommentFixture(Base):
    __tablename__ = 'scope_comment_fixture'
    id = Column(Integer, primary_key=True)
    municipio_ticket_id = Column(Integer, ForeignKey('scope_municipal_fixture.id'))
    pyme_ticket_id = Column(Integer, ForeignKey('scope_business_fixture.id'))
    comentario = Column(Text)
    fecha = Column(DateTime)
    origen = Column(String(20))
    user_id = Column(Integer)
    anon_id = Column(String(80))
    es_admin = Column(Integer)
    estado_ticket = Column(String(30))

    def to_dict(self):
        return {
            'id': self.id, 'comentario': self.comentario, 'fecha': self.fecha.isoformat(),
            'es_admin': bool(self.es_admin), 'origen': self.origen,
            'user_id': self.user_id, 'anon_id': self.anon_id,
            'municipio_ticket_id': self.municipio_ticket_id, 'pyme_ticket_id': self.pyme_ticket_id,
        }


def _load_functions(path: Path, names: set[str], namespace: dict, *, class_name: str | None = None):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    nodes = tree.body
    if class_name:
        nodes = next(node for node in nodes if isinstance(node, ast.ClassDef) and node.name == class_name).body
    functions = [node for node in nodes if isinstance(node, ast.FunctionDef) and node.name in names]
    if {node.name for node in functions} != names:
        raise AssertionError('Expected exact source functions were not found')
    for node in functions:
        node.decorator_list = []
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), 'exec'), namespace)


class TicketPublicHistoryScopeTest(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine)
        self.selects = []
        event.listen(self.engine, 'before_cursor_execute', self._record_select)
        LegacyFixture.query = self.session.query(LegacyFixture)
        CommentFixture.query = self.session.query(CommentFixture)
        MunicipalFixture.query = self.session.query(MunicipalFixture)
        BusinessFixture.query = self.session.query(BusinessFixture)
        self.when = datetime(2026, 10, 14, 12)
        self.municipal = self._ticket(MunicipalFixture, 1, 101, '100001')
        self.business = self._ticket(BusinessFixture, 1, 201, '200001')
        self.other = self._ticket(MunicipalFixture, 2, 102, '100002')
        self.session.add_all([self.municipal, self.business, self.other])
        self.session.flush()
        self.session.add_all([
            self._comment(1, 'comentario-publico-vinculado', municipio=1, minute=1),
            self._comment(2, 'nota-interna-vinculada', municipio=1, origin=' INTERNAL ', minute=2),
            self._comment(3, 'otro-caso-municipal', municipio=2, minute=3),
            self._comment(4, 'comentario-pyme-vinculado', pyme=1, minute=4),
            self._comment(5, 'comentario-pyme-interno', pyme=1, origin='internal', minute=5),
            LegacyFixture(session_id='legacy-same-anon', pyme_id=11, pregunta='antes-legitimo-no-enlazado', respuesta='respuesta-no-enlazada', timestamp=self.when - timedelta(minutes=1)),
            LegacyFixture(session_id='legacy-same-anon', pyme_id=22, pregunta='otro-negocio-no-enlazado', respuesta='otra-respuesta', timestamp=self.when),
            LegacyFixture(session_id='legacy-same-anon', pyme_id=11, pregunta='otro-asunto-futuro', respuesta='respuesta-futura', timestamp=self.when + timedelta(days=1)),
            LegacyFixture(session_id='otro-anon', pyme_id=11, pregunta='sesion-diferente', respuesta='otra-sesion', timestamp=self.when),
        ])
        self.session.commit()
        self.service_scope = {
            'Union': Union, 'MunicipioTicket': MunicipalFixture, 'PymeTicket': BusinessFixture,
            'Conversacion': LegacyFixture, 'TicketComentario': CommentFixture,
            'datetime_to_iso_utc': lambda value: value.isoformat() if value else None,
            'timedelta': timedelta, 'build_identity_subject': lambda **kw: kw,
            '_is_internal_ticket_comment': lambda row: str(row.origen or '').strip().casefold() == 'internal',
            '_public_ticket_comment_filter': lambda: or_(CommentFixture.origen.is_(None), func.lower(func.trim(CommentFixture.origen)) != 'internal'),
        }
        _load_functions(ROOT / 'services/ticket_service.py', {'obtener_historial_chat'}, self.service_scope, class_name='ServicioTickets')
        self.history = self.service_scope['obtener_historial_chat']
        self.history_calls = []
        self.route_scope = self._route_scope()
        _load_functions(ROOT / 'routes/ticket.py', {
            '_resolver_acceso_chat_ticket', '_resolve_ticket_with_access', '_public_ticket_history_scope',
            '_serialize_ticket_details', 'get_ticket_timeline', 'get_ticket_by_number_public',
            '_unique_municipio_ticket_from_query', '_ticket_private_no_store_response',
            'get_ticket_details', 'get_ticket_details_pyme',
        }, self.route_scope)
        self.actor = None
        self.app = Flask('ticket-public-scope-source-fixture')
        self.app.add_url_rule('/tickets/<tipo>/<int:ticket_id>/timeline', 'timeline',
            lambda tipo, ticket_id: self.route_scope['get_ticket_timeline'](self.actor, tipo, ticket_id))
        self.app.add_url_rule('/tickets/municipio/por_numero/<number>', 'number',
            lambda number: self.route_scope['get_ticket_by_number_public'](self.actor, None, None, number))
        self.app.add_url_rule('/tickets/municipio/<int:ticket_id>', 'private_municipal',
            lambda ticket_id: self.route_scope['get_ticket_details'](self.actor, ticket_id))
        self.app.add_url_rule('/tickets/pyme/<int:ticket_id>', 'private_business',
            lambda ticket_id: self.route_scope['get_ticket_details_pyme'](self.actor, ticket_id))
        self.client = self.app.test_client()
        self.selects.clear()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()

    def _record_select(self, _conn, _cursor, statement, *_args):
        if statement.lstrip().upper().startswith('SELECT'):
            self.selects.append(statement)

    def _ticket(self, model, number, tenant, code):
        ticket = model(id=number, tenant_id=tenant, user_id=None, anon_id='legacy-same-anon', nro_ticket=code, consulta_pin='654321', fecha=self.when, estado='nuevo')
        ticket.municipio_id = 11 if model is MunicipalFixture else None
        ticket.pregunta = 'Solicitud vinculada'
        ticket.asunto = 'Consulta'
        ticket.categoria = 'general'
        ticket.ultima_actividad = self.when
        ticket.latitud = ticket.longitud = None
        ticket.archivos = []
        ticket.comentarios = self.session.query(CommentFixture).filter(
            (CommentFixture.municipio_ticket_id if model is MunicipalFixture else CommentFixture.pyme_ticket_id) == number)
        return ticket

    def _comment(self, number, text, *, municipio=None, pyme=None, origin='chat', minute=0):
        return CommentFixture(id=number, municipio_ticket_id=municipio, pyme_ticket_id=pyme, comentario=text, fecha=self.when + timedelta(minutes=minute), origen=origin, es_admin=0)

    def _call_history(self, ticket, **kw):
        self.history_calls.append(dict(kw))
        return self.history(None, ticket, **kw)

    def _route_scope(self):
        private_scope = lambda _type, ticket, actor: bool(actor and actor.tenant_id == ticket.tenant_id)
        service = SimpleNamespace(
            obtener_historial_chat=self._call_history,
            obtener_timeline_ticket=lambda ticket, **kw: [{'tipo': 'ticket_creado', 'estado': 'nuevo', 'fecha': self.when.isoformat()}],
            obtener_estado_progreso=lambda *_args, **_kw: [],
        )
        return {
            'Optional': Optional, 'User': SimpleNamespace, 'MunicipioTicket': MunicipalFixture, 'PymeTicket': BusinessFixture,
            'TicketComentario': CommentFixture,
            'db': SimpleNamespace(session=self.session), 'jsonify': jsonify, 'request': request, 'g': g,
            'make_response': make_response, 'current_app': current_app, 'servicio_tickets': service,
            'MENSAJE_SIN_PERMISOS': 'Acceso denegado.', '_ticket_scope_access_allows': private_scope,
            '_ticket_request_id': lambda: 'ticket-scope-fixture', '_request_anon_id': lambda: None,
            '_effective_ticket_actor': lambda current, owner: current, '_request_contact_key': lambda: None,
            '_validar_asignacion_empleado': lambda *_: None,
            '_safe_ticket_realtime_summary': lambda *_args, **_kw: {'read_state': {'latest_comment_id': 1}},
            'build_unified_conversation_stream': lambda **kw: kw['historial_chat'],
            '_ticket_degraded_meta': lambda reasons: {'degraded': bool(reasons), 'degraded_reasons': list(reasons)},
            '_ticket_json': lambda payload, **kw: jsonify(payload),
            'verify_recaptcha': lambda *_: True,
            '_authenticated_municipio_number_query': lambda code, actor: MunicipalFixture.query.filter_by(nro_ticket=code, tenant_id=actor.tenant_id),
            '_authenticated_municipio_lookup_allows': lambda ticket, actor: private_scope('municipio', ticket, actor),
            '_get_user_info': lambda *_: dict(nombre='Ciudadano', telefono='', email='', direccion='', dni='', descripcion='Solicitud'),
            '_ticket_contact_identity': lambda *_: {}, '_identity_visual_fields': lambda *_: {},
            '_public_ticket_comment_filter': self.service_scope['_public_ticket_comment_filter'],
            '_is_internal_ticket_comment': self.service_scope['_is_internal_ticket_comment'],
            '_safe_ticket_comment_payload': lambda comment, **kw: {**comment.to_dict(), 'texto': comment.comentario},
            '_build_ticket_operational_badges': lambda *_: {'sla_status': None, 'badges': [], 'age_hours': 0, 'inactivity_hours': 0},
            '_ticket_location_payload': lambda *_: dict(direccion=None, latitud=None, longitud=None, coordinates=None, map_search_url=None, has_location=False, location=None),
            '_ticket_priority_payload': lambda *_: {'priority': None, 'priority_score': 0, 'priority_breakdown': []},
            'build_ticket_collaboration_state': lambda **kw: {}, '_ticket_crm_queue_payload': lambda *_args, **_kw: {},
            'resolve_municipio_category_authority': lambda *_: None,
            '_generate_friendly_ticket_id': lambda ticket, kind: f'{kind}:{ticket.nro_ticket}',
            'datetime_to_iso_utc': lambda value: value.isoformat() if value else None,
            'serialize_attachment_for_delivery': lambda attachment, **kw: {},
            '_private_ticket_workflow': lambda *_: None,
        }

    def _texts(self, response):
        self.assertEqual(response.status_code, 200)
        return [item['texto'] for item in response.get_json()['historial_chat']]

    def _assert_public(self, response, expected):
        self.assertEqual(self._texts(response), expected)
        body = response.get_json()
        self.assertEqual(body['history_scope'], {
            'contract_version': 'tickets.history_scope.v1', 'scope': 'ticket_comments_only',
            'legacy_conversations_included': False, 'retryable': False,
            'notice': 'Esta consulta muestra los mensajes vinculados a la solicitud.',
        })
        self.assertFalse(body['meta']['degraded'])
        self.assertFalse(any('scope_legacy_fixture' in sql for sql in self.selects))

    def test_public_number_pin_keeps_linked_comment_and_no_legacy_query(self):
        response = self.client.get('/tickets/municipio/por_numero/M-100001?pin=654321')
        self._assert_public(response, ['comentario-publico-vinculado'])
        self.assertEqual(response.headers.get('Cache-Control'), 'private, no-store, max-age=0')

    def test_public_municipal_timeline_keeps_later_linked_comment(self):
        self._assert_public(self.client.get('/tickets/municipio/1/timeline?pin=654321'), ['comentario-publico-vinculado'])

    def test_public_business_same_integer_id_does_not_mix_case_type(self):
        self._assert_public(self.client.get('/tickets/pyme/1/timeline?pin=654321'), ['comentario-pyme-vinculado'])

    def test_no_anon_still_keeps_linked_public_comment_and_descriptor(self):
        self.municipal.anon_id = None
        self.session.commit()
        self.selects.clear()
        self._assert_public(self.client.get('/tickets/municipio/1/timeline?pin=654321'), ['comentario-publico-vinculado'])

    def test_missing_pin_number_still_400(self):
        response = self.client.get('/tickets/municipio/por_numero/100001')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.history_calls, [])

    def test_bad_pin_number_still_masked_404(self):
        response = self.client.get('/tickets/municipio/por_numero/100001?pin=000000')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.history_calls, [])

    def test_unknown_number_still_masked_404(self):
        response = self.client.get('/tickets/municipio/por_numero/999999?pin=654321')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.history_calls, [])

    def test_bad_pin_timeline_still_403(self):
        response = self.client.get('/tickets/municipio/1/timeline?pin=000000')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.history_calls, [])

    def test_unknown_case_timeline_still_404(self):
        response = self.client.get('/tickets/municipio/999/timeline?pin=654321')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.history_calls, [])

    def test_invalid_type_timeline_still_400(self):
        response = self.client.get('/tickets/unsupported/1/timeline?pin=654321')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.history_calls, [])

    def test_private_municipal_detail_preserves_legacy_and_internal(self):
        self.actor = SimpleNamespace(id=901, tenant_id=101)
        response = self.client.get('/tickets/municipio/1')
        texts = self._texts(response)
        self.assertIn('antes-legitimo-no-enlazado', texts)
        self.assertIn('nota-interna-vinculada', texts)
        self.assertNotIn('history_scope', response.get_json())
        self.assertTrue(any('scope_legacy_fixture' in sql for sql in self.selects))

    def test_private_business_detail_preserves_default_legacy(self):
        self.actor = SimpleNamespace(id=902, tenant_id=201)
        response = self.client.get('/tickets/pyme/1')
        texts = self._texts(response)
        self.assertIn('antes-legitimo-no-enlazado', texts)
        self.assertIn('comentario-pyme-interno', texts)
        self.assertNotIn('history_scope', response.get_json())

    def test_private_agent_timeline_preserves_legacy_and_internal(self):
        self.actor = SimpleNamespace(id=901, tenant_id=101)
        response = self.client.get('/tickets/municipio/1/timeline')
        texts = self._texts(response)
        self.assertIn('otro-asunto-futuro', texts)
        self.assertIn('nota-interna-vinculada', texts)
        self.assertNotIn('history_scope', response.get_json())

    def test_private_wrong_tenant_details_still_404_without_history(self):
        self.actor = SimpleNamespace(id=901, tenant_id=102)
        response = self.client.get('/tickets/municipio/1')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.history_calls, [])

    def test_method_default_remains_compatible_private_history(self):
        texts = [item['texto'] for item in self.history(None, self.municipal)]
        self.assertIn('antes-legitimo-no-enlazado', texts)
        self.assertIn('nota-interna-vinculada', texts)
        self.assertNotIn('sesion-diferente', texts)

    def test_method_public_omits_null_and_foreign_owner_without_guessing(self):
        self.session.add(LegacyFixture(session_id='legacy-same-anon', pyme_id=None, pregunta='municipio-null', respuesta='no-atribuible', timestamp=self.when))
        self.session.commit()
        self.selects.clear()
        items = self.history(None, self.municipal, include_internal=False, include_legacy_conversations=False)
        self.assertEqual([item['texto'] for item in items], ['comentario-publico-vinculado'])
        self.assertFalse(any('scope_legacy_fixture' in sql for sql in self.selects))

    def test_method_public_empty_case_does_not_fall_back_to_global_history(self):
        self.session.query(CommentFixture).filter_by(municipio_ticket_id=2).delete()
        self.session.commit()
        self.selects.clear()
        self.assertEqual(self.history(None, self.other, include_internal=False, include_legacy_conversations=False), [])
        self.assertFalse(any('scope_legacy_fixture' in sql for sql in self.selects))

    def test_scope_descriptor_is_fresh_each_call(self):
        first = self.route_scope['_public_ticket_history_scope']()
        first['notice'] = 'modified-fixture'
        self.assertEqual(self.route_scope['_public_ticket_history_scope']()['notice'], 'Esta consulta muestra los mensajes vinculados a la solicitud.')

    def test_public_dialogue_retains_ordered_staff_and_citizen_case_comments(self):
        staff = self._comment(10, 'respuesta-publica-del-equipo', municipio=1, origin='admin_panel', minute=10)
        staff.es_admin = 1
        self.session.add_all([staff, self._comment(11, 'respuesta-ciudadana-vinculada', municipio=1, minute=11)])
        self.session.commit()
        self.selects.clear()
        self._assert_public(self.client.get('/tickets/municipio/1/timeline?pin=654321'), [
            'comentario-publico-vinculado', 'respuesta-publica-del-equipo', 'respuesta-ciudadana-vinculada'])

    def test_nonagent_authenticated_owner_uses_same_public_comment_scope(self):
        self.actor = SimpleNamespace(id=701, tenant_id=999)
        self.municipal.user_id = 701
        self.session.commit()
        self.selects.clear()
        self._assert_public(self.client.get('/tickets/municipio/1/timeline'), ['comentario-publico-vinculado'])


if __name__ == '__main__':
    unittest.main()
