import unittest
from types import SimpleNamespace
from unittest.mock import patch, call

from socket_service import (
    emit_new_ticket,
    emit_new_chat_message,
    emit_ticket_comment,
    emit_ticket_status_changed,
    emit_ticket_assignment_changed,
    emit_ticket_presence_changed,
    emit_conversation_message_read,
    emit_conversation_linked,
    emit_ticket_unread_changed,
    emit_ticket_reply_delivery_updated,
    emit_crm_contact_update,
    emit_crm_notification_update,
    emit_survey_update,
)


class SocketServiceEventTests(unittest.TestCase):
    _INVALIDATION = {
        'contract_version': 'tickets.collection.invalidated.v1',
        'resource': 'tickets',
        'reason': 'collection_changed',
        'refetch': True,
    }

    def _assert_opaque_invalidation(self, emitted, *, room):
        self.assertEqual(emitted, call('ticket_update', self._INVALIDATION, room=room))
        serialized = str(emitted.args[1])
        for marker in (
            'ticket_id', 'ticketId', 'tenant_type', 'categoria', 'descripcion',
            'direccion', 'email', 'telefono', 'dni', 'actor', 'assigned_to',
            'presence_status', 'last_read_comment_id', 'unread_viewer_count',
        ):
            self.assertNotIn(marker, serialized)

    def test_emit_new_ticket_emits_only_opaque_scoped_invalidation(self):
        payload = {
            "tenant_type": "municipio",
            "municipio_id": 7,
            "id": 11,
            "categoria": "salud",
            "descripcion": "private-ticket-description",
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_new_ticket(payload)

        self.assertEqual(len(mock_emit.call_args_list), 1)
        self._assert_opaque_invalidation(mock_emit.call_args, room='municipio_7')

    def test_reply_delivery_event_is_opaque_and_only_requests_refetch(self):
        private_payload = {
            "tenant_id": 7,
            "ticket_id": 419,
            "event_id": "private-reply-event",
            "provider_message_id": "SM-private-provider-id",
            "status": "read",
            "body": "contenido privado del vecino",
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_reply_delivery_updated(private_payload)

        self.assertEqual(len(mock_emit.call_args_list), 2)
        delivery_event = mock_emit.call_args_list[0]
        self.assertEqual(delivery_event.args[0], 'ticket.reply.delivery.updated')
        self.assertEqual(delivery_event.kwargs, {'room': 'tenant_7'})
        self.assertEqual(
            delivery_event.args[1],
            {
                'contract_version': 'tenant_ticket.reply_delivery.realtime.v1',
                'resource': 'reply_deliveries',
                'reason': 'delivery_status_changed',
                'refetch': True,
            },
        )
        self._assert_opaque_invalidation(
            mock_emit.call_args_list[1], room='tenant_7'
        )
        serialized = str(mock_emit.call_args_list)
        self.assertNotIn('private-reply-event', serialized)
        self.assertNotIn('SM-private-provider-id', serialized)
        self.assertNotIn('contenido privado del vecino', serialized)

    def test_emit_ticket_comment_prefers_explicit_room(self):
        payload = {
            "socket_room": "tenant_3",
            "tenant_type": "pyme",
            "tenant_id": 3,
            "ticket_id": 15,
            "ticket": {"contacto": {"email": "private@example.com"}, "ai_summary": "internal"},
            "comment": {
                "id": 81,
                "comentario": "Respuesta publica",
                "texto": "Respuesta publica",
                "origen": "chat",
                "es_admin": True,
                "user_id": 44,
                "anon_id": "private-anon",
            },
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_comment(payload)

        public_event = mock_emit.call_args_list[0]
        self.assertEqual(public_event.args[0], 'new_chat_message')
        self.assertEqual(public_event.args[1]['socket_room'], 'ticket_pyme_15')
        self.assertEqual(public_event.args[1]['contract_version'], 'live_chat.public_message.v1')
        self.assertEqual(public_event.args[1]['message']['comentario'], 'Respuesta publica')
        self.assertNotIn('ticket', public_event.args[1])
        self.assertNotIn('tenant_id', public_event.args[1])
        self.assertNotIn('user_id', public_event.args[1]['message'])
        self.assertNotIn('anon_id', public_event.args[1]['message'])
        self.assertEqual(public_event.kwargs, {'room': 'ticket_pyme_15'})
        tenant_event = mock_emit.call_args_list[1]
        self.assertEqual(tenant_event.args[0], 'ticket_update')
        self.assertEqual(tenant_event.kwargs, {'room': 'tenant_3'})
        self.assertEqual(tenant_event.args[1]['refetch'], True)
        self.assertNotIn('ticket_id', tenant_event.args[1])
        self.assertNotIn('Respuesta publica', str(tenant_event.args[1]))
        self.assertEqual(len(mock_emit.call_args_list), 2)

    def test_emit_ticket_comment_never_mirrors_internal_notes_to_public_room(self):
        payload = {
            "socket_room": "tenant_3",
            "tenant_type": "pyme",
            "ticket_id": 15,
            "comment": {
                "id": 82,
                "comentario": "Nota solo para operadores",
                "origen": "internal",
                "es_admin": True,
            },
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_comment(payload)

        emitted_names = [item.args[0] for item in mock_emit.call_args_list]
        self.assertNotIn('new_chat_message', emitted_names)
        self.assertEqual(emitted_names, ['ticket_update'])
        self.assertEqual(mock_emit.call_args.kwargs, {'room': 'tenant_3'})
        self.assertNotIn('Nota solo para operadores', str(mock_emit.call_args.args[1]))

    def test_emit_ticket_status_changed_keeps_broad_room_opaque(self):
        payload = {
            "socket_room": "municipio_7",
            "tenant_type": "municipio",
            "ticket_id": 11,
            "estado": "en_proceso",
            "descripcion": "private-status-description",
            "actor": {"email": "private-status@example.com"},
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_status_changed(payload)

        self.assertEqual(len(mock_emit.call_args_list), 2)
        self._assert_opaque_invalidation(mock_emit.call_args_list[0], room='municipio_7')
        public_event = mock_emit.call_args_list[1]
        self.assertEqual(public_event.args[0], 'ticket.status.changed')
        self.assertEqual(public_event.kwargs, {'room': 'ticket_municipio_11'})
        self.assertEqual(public_event.args[1]['contract_version'], 'live_chat.public_state.v1')
        self.assertEqual(public_event.args[1]['estado'], 'en_proceso')
        self.assertNotIn('private-status-description', str(public_event.args[1]))
        self.assertNotIn('private-status@example.com', str(public_event.args[1]))

    def test_public_ticket_state_accepts_traditional_id_field(self):
        payload = {
            "socket_room": "municipio_7",
            "tenant_type": "municipio",
            "id": 11,
            "estado": "resuelto",
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_status_changed(payload)

        public_event = next(
            item
            for item in mock_emit.call_args_list
            if item.args[0] == 'ticket.status.changed'
            and item.kwargs.get('room') == 'ticket_municipio_11'
        )
        self.assertEqual(public_event.args[1]['ticket_id'], 11)
        self.assertEqual(public_event.args[1]['ticketId'], 11)
        self.assertEqual(public_event.args[1]['estado'], 'resuelto')

    def test_emit_ticket_assignment_changed_keeps_broad_room_opaque(self):
        payload = {
            "socket_room": "municipio_7",
            "tenant_type": "municipio",
            "ticket_id": 11,
            "assignment_state": "assigned",
            "assigned_to": {"id": 22, "email": "private-assignee@example.com"},
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_assignment_changed(payload)

        self.assertEqual(len(mock_emit.call_args_list), 2)
        self._assert_opaque_invalidation(mock_emit.call_args_list[0], room='municipio_7')
        public_event = mock_emit.call_args_list[1]
        self.assertEqual(public_event.args[0], 'ticket.assignment.changed')
        self.assertEqual(public_event.kwargs, {'room': 'ticket_municipio_11'})
        self.assertEqual(public_event.args[1]['assignment_state'], 'assigned')
        self.assertNotIn('private-assignee@example.com', str(public_event.args[1]))

    def test_emit_new_chat_message_sanitizes_public_and_invalidates_tenant(self):
        payload = {
            "socket_room": "ticket_municipio_11",
            "tenant_type": "municipio",
            "municipio_id": 7,
            "ticket_id": 11,
            "channel": "whatsapp",
            "message": {
                "id": 91,
                "comentario": "Respuesta del operador",
                "texto": "Respuesta del operador",
                "origen": "chat",
                "es_admin": True,
                "user_id": 44,
                "anon_id": "private-anon",
                "actor_identity": {
                    "email": "operator@example.com",
                    "phone": "+5491111111111",
                },
            },
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_new_chat_message(payload)

        public_message = mock_emit.call_args_list[0]
        self.assertEqual(public_message.args[0], 'new_chat_message')
        self.assertEqual(public_message.kwargs, {'room': 'ticket_municipio_11'})
        self.assertEqual(public_message.args[1]['contract_version'], 'live_chat.public_message.v1')
        self.assertEqual(public_message.args[1]['message']['comentario'], 'Respuesta del operador')
        self.assertNotIn('user_id', public_message.args[1]['message'])
        self.assertNotIn('anon_id', public_message.args[1]['message'])
        self.assertNotIn('actor_identity', public_message.args[1]['message'])
        tenant_event = mock_emit.call_args_list[1]
        self.assertEqual(tenant_event.args[0], 'ticket_update')
        self.assertEqual(tenant_event.kwargs, {'room': 'municipio_7'})
        self.assertEqual(
            tenant_event.args[1],
            {
                'contract_version': 'tickets.collection.invalidated.v1',
                'resource': 'tickets',
                'reason': 'collection_changed',
                'refetch': True,
            },
        )
        tenant_serialized = str(tenant_event.args[1])
        self.assertNotIn('Respuesta del operador', tenant_serialized)
        self.assertNotIn('operator@example.com', tenant_serialized)
        self.assertNotIn('ticket_id', tenant_event.args[1])
        self.assertEqual(len(mock_emit.call_args_list), 2)

    def test_emit_ticket_presence_changed_keeps_broad_room_opaque(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "presence_status": "active"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_presence_changed(payload)

        self._assert_opaque_invalidation(mock_emit.call_args, room='municipio_7')

    def test_emit_conversation_message_read_keeps_broad_room_opaque(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "last_read_comment_id": 55, "read_at": "2026-03-21T00:00:00+00:00"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_conversation_message_read(payload)

        self._assert_opaque_invalidation(mock_emit.call_args, room='municipio_7')

    def test_emit_conversation_linked_keeps_broad_room_opaque(self):
        payload = {
            "socket_room": "municipio_7",
            "tenant_type": "municipio",
            "ticket_id": 11,
            "target_identity": "private-neighbor@example.com",
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_conversation_linked(payload)

        self._assert_opaque_invalidation(mock_emit.call_args, room='municipio_7')
        self.assertNotIn('private-neighbor@example.com', str(mock_emit.call_args.args[1]))

    def test_emit_ticket_unread_changed_keeps_broad_room_opaque(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "summary": {"unread_viewer_count": 2}}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_unread_changed(payload)

        self._assert_opaque_invalidation(mock_emit.call_args, room='municipio_7')

    def test_crm_broad_rooms_receive_only_opaque_collection_invalidations(self):
        tenant = SimpleNamespace(id=3, slug='junin')
        private_payload = {
            'email': 'private-neighbor@example.com',
            'phone': '+5491111111111',
            'body': 'private notification body',
        }

        with patch(
            'socket_service._get_rooms_for_tenant_slug',
            return_value=['crm_3', 'tenant_3'],
        ), patch('socket_service.socketio.emit') as mock_emit:
            emit_crm_contact_update(tenant, private_payload)
            emit_crm_notification_update(tenant, private_payload)

        self.assertEqual(len(mock_emit.call_args_list), 8)
        for emitted in mock_emit.call_args_list:
            payload = emitted.args[1]
            self.assertEqual(payload['contract_version'], 'collections.invalidated.v1')
            self.assertIn(payload['resource'], {'contacts', 'notifications'})
            self.assertEqual(payload['reason'], 'collection_changed')
            self.assertTrue(payload['refetch'])
            serialized = str(payload)
            self.assertNotIn('private-neighbor@example.com', serialized)
            self.assertNotIn('+5491111111111', serialized)
            self.assertNotIn('private notification body', serialized)
            self.assertIn(emitted.kwargs['room'], {'crm_3', 'tenant_3'})

    def test_emit_survey_update_emits_legacy_and_v2_payloads_to_tenant_room(self):
        legacy_payload = {"total_respuestas": 1, "preguntas": {"10": {"opciones": []}}}
        modern_payload = {
            "contract_version": "surveys.live_results.v2",
            "result_version": 42,
            "total_respuestas": 1,
            "preguntas": [{"id": 10, "opciones": []}],
            "legacy_results": legacy_payload,
        }

        with patch('socket_service._resolve_survey_tenant_slug', return_value='junin'), patch(
            'socket_service.socketio.emit'
        ) as mock_emit:
            emit_survey_update("consulta-barrial", modern_payload, tenant_slug="junin")

        self.assertEqual(mock_emit.call_args_list[0], call('survey_update', legacy_payload, room='encuesta:junin:consulta-barrial'))
        event_name, event_payload = mock_emit.call_args_list[1].args[:2]
        self.assertEqual(event_name, 'survey_update_v2')
        self.assertEqual(event_payload["contract_version"], "surveys.live_results.v2")
        self.assertEqual(event_payload["result_version"], 42)
        self.assertNotIn("legacy_results", event_payload)
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'encuesta:junin:consulta-barrial'})
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'survey.vote.created')
        self.assertEqual(mock_emit.call_args_list[2].args[1]["result_version"], 42)
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'encuesta:junin:consulta-barrial'})

    def test_emit_survey_update_never_emits_unscoped_legacy_room(self):
        legacy_payload = {"total_respuestas": 1}
        modern_payload = {
            "contract_version": "surveys.live_results.v2",
            "result_version": 43,
            "tenant_slug": "junin",
            "total_respuestas": 1,
            "legacy_results": legacy_payload,
        }

        with patch('socket_service._resolve_survey_tenant_slug', return_value='junin'), patch(
            'socket_service.socketio.emit'
        ) as mock_emit:
            emit_survey_update("consulta-barrial", modern_payload, tenant_slug="junin")

        self.assertEqual(mock_emit.call_args_list[0], call('survey_update', legacy_payload, room='encuesta:junin:consulta-barrial'))
        self.assertEqual(mock_emit.call_args_list[1].args[0], 'survey_update_v2')
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'encuesta:junin:consulta-barrial'})
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'survey.vote.created')
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'encuesta:junin:consulta-barrial'})
        self.assertEqual(len(mock_emit.call_args_list), 3)
        self.assertNotIn('encuesta_consulta-barrial', {item.kwargs.get('room') for item in mock_emit.call_args_list})

    def test_emit_survey_update_drops_event_when_tenant_cannot_be_resolved(self):
        with patch('socket_service._resolve_survey_tenant_slug', return_value=''), patch(
            'socket_service.socketio.emit'
        ) as mock_emit:
            emit_survey_update("ambiguous-survey", {"total_respuestas": 1})

        mock_emit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
