import unittest
from unittest.mock import patch, call

from socket_service import (
    emit_new_ticket,
    emit_new_chat_message,
    emit_ticket_comment,
    emit_ticket_status_changed,
    emit_ticket_assignment_changed,
    emit_ticket_presence_changed,
    emit_conversation_message_read,
    emit_ticket_unread_changed,
    emit_survey_update,
)


class SocketServiceEventTests(unittest.TestCase):
    def test_emit_new_ticket_emits_scoped_events(self):
        payload = {"tenant_type": "municipio", "municipio_id": 7, "id": 11}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_new_ticket(payload)

        mock_emit.assert_has_calls(
            [
                call('new_ticket', payload, room='municipio_7'),
                call('ticket_update', payload, room='municipio_7'),
            ]
        )
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'ticket.updated')
        self.assertEqual(mock_emit.call_args_list[2].args[1]['room'], 'municipio_7')
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'municipio_7'})

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

        assert mock_emit.call_args_list[0] == call('new_comment', payload, room='tenant_3')
        event_name, event_payload = mock_emit.call_args_list[1].args[:2]
        self.assertEqual(event_name, 'conversation.message.created')
        self.assertEqual(event_payload['room'], 'tenant_3')
        self.assertEqual(event_payload['ticket']['id'], 15)
        self.assertEqual(event_payload['ticket']['tenant_type'], 'pyme')
        self.assertEqual(event_payload['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'tenant_3'})
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'ticket.message.created')
        self.assertEqual(mock_emit.call_args_list[2].args[1]['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'tenant_3'})
        public_event = mock_emit.call_args_list[3]
        self.assertEqual(public_event.args[0], 'new_chat_message')
        self.assertEqual(public_event.args[1]['socket_room'], 'ticket_pyme_15')
        self.assertEqual(public_event.args[1]['contract_version'], 'live_chat.public_message.v1')
        self.assertEqual(public_event.args[1]['message']['comentario'], 'Respuesta publica')
        self.assertNotIn('ticket', public_event.args[1])
        self.assertNotIn('tenant_id', public_event.args[1])
        self.assertNotIn('user_id', public_event.args[1]['message'])
        self.assertNotIn('anon_id', public_event.args[1]['message'])
        self.assertEqual(public_event.kwargs, {'room': 'ticket_pyme_15'})

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
        self.assertEqual(emitted_names, [
            'new_comment',
            'conversation.message.created',
            'ticket.message.created',
        ])

    def test_emit_ticket_status_changed_emits_legacy_and_standard_events(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "estado": "en_proceso"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_status_changed(payload)

        event_name, event_payload = mock_emit.call_args_list[0].args[:2]
        self.assertEqual(event_name, 'ticket.status.changed')
        self.assertEqual(event_payload['room'], 'municipio_7')
        self.assertEqual(event_payload['ticket']['id'], 11)
        self.assertEqual(event_payload['ticket']['status'], 'en_proceso')
        self.assertEqual(event_payload['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[0].kwargs, {'room': 'municipio_7'})
        self.assertEqual(mock_emit.call_args_list[1], call('ticket_update', payload, room='municipio_7'))
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'ticket.updated')
        self.assertEqual(mock_emit.call_args_list[2].args[1]['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'municipio_7'})

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

    def test_emit_ticket_assignment_changed_emits_legacy_and_standard_events(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "assigned_to": {"id": 22}}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_assignment_changed(payload)

        event_name, event_payload = mock_emit.call_args_list[0].args[:2]
        self.assertEqual(event_name, 'ticket.assignment.changed')
        self.assertEqual(event_payload['room'], 'municipio_7')
        self.assertEqual(event_payload['ticket']['id'], 11)
        self.assertEqual(event_payload['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[0].kwargs, {'room': 'municipio_7'})
        self.assertEqual(mock_emit.call_args_list[1], call('ticket_update', payload, room='municipio_7'))
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'ticket.updated')
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'municipio_7'})

    def test_emit_new_chat_message_emits_whatsapp_analytics_alias(self):
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
        self.assertEqual(mock_emit.call_args_list[1], call('new_chat_message', payload, room='municipio_7'))
        self.assertEqual(mock_emit.call_args_list[2].args[0], 'conversation.message.created')
        self.assertEqual(mock_emit.call_args_list[2].args[1]['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[2].kwargs, {'room': 'municipio_7'})
        self.assertEqual(mock_emit.call_args_list[3].args[0], 'ticket.message.created')
        self.assertEqual(mock_emit.call_args_list[3].args[1]['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[3].kwargs, {'room': 'municipio_7'})
        self.assertEqual(mock_emit.call_args_list[4].args[0], 'whatsapp.message.created')
        self.assertEqual(mock_emit.call_args_list[4].args[1]['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[4].kwargs, {'room': 'municipio_7'})
        self.assertEqual(len(mock_emit.call_args_list), 5)

    def test_emit_ticket_presence_changed_uses_enterprise_envelope(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "presence_status": "active"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_presence_changed(payload)

        event_name, event_payload = mock_emit.call_args.args[:2]
        self.assertEqual(event_name, 'ticket.presence.changed')
        self.assertEqual(event_payload['ticket']['id'], 11)
        self.assertEqual(event_payload['payload']['presence_status'], 'active')
        self.assertEqual(mock_emit.call_args.kwargs, {'room': 'municipio_7'})

    def test_emit_conversation_message_read_uses_enterprise_envelope(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "last_read_comment_id": 55, "read_at": "2026-03-21T00:00:00+00:00"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_conversation_message_read(payload)

        event_name, event_payload = mock_emit.call_args.args[:2]
        self.assertEqual(event_name, 'conversation.message.read')
        self.assertEqual(event_payload['ticket']['id'], 11)
        self.assertEqual(event_payload['message']['read_at'], '2026-03-21T00:00:00+00:00')
        self.assertEqual(event_payload['payload']['last_read_comment_id'], 55)
        self.assertEqual(mock_emit.call_args.kwargs, {'room': 'municipio_7'})

    def test_emit_ticket_unread_changed_uses_enterprise_envelope(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "summary": {"unread_viewer_count": 2}}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_unread_changed(payload)

        event_name, event_payload = mock_emit.call_args.args[:2]
        self.assertEqual(event_name, 'ticket.unread.changed')
        self.assertEqual(event_payload['ticket']['id'], 11)
        self.assertEqual(event_payload['payload']['summary']['unread_viewer_count'], 2)
        self.assertEqual(mock_emit.call_args.kwargs, {'room': 'municipio_7'})

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
