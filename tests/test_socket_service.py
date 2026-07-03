import unittest
from unittest.mock import patch, call

from socket_service import (
    emit_new_ticket,
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

    def test_emit_ticket_comment_prefers_explicit_room(self):
        payload = {"socket_room": "pyme_3", "tenant_type": "pyme", "ticket_id": 15}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_comment(payload)

        assert mock_emit.call_args_list[0] == call('new_comment', payload, room='pyme_3')
        event_name, event_payload = mock_emit.call_args_list[1].args[:2]
        self.assertEqual(event_name, 'conversation.message.created')
        self.assertEqual(event_payload['room'], 'pyme_3')
        self.assertEqual(event_payload['ticket']['id'], 15)
        self.assertEqual(event_payload['ticket']['tenant_type'], 'pyme')
        self.assertEqual(event_payload['payload'], payload)
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'pyme_3'})

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

    def test_emit_survey_update_emits_legacy_and_v2_payloads(self):
        legacy_payload = {"total_respuestas": 1, "preguntas": {"10": {"opciones": []}}}
        modern_payload = {
            "contract_version": "surveys.live_results.v2",
            "result_version": 42,
            "total_respuestas": 1,
            "preguntas": [{"id": 10, "opciones": []}],
            "legacy_results": legacy_payload,
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_survey_update("consulta-barrial", modern_payload)

        self.assertEqual(mock_emit.call_args_list[0], call('survey_update', legacy_payload, room='encuesta_consulta-barrial'))
        event_name, event_payload = mock_emit.call_args_list[1].args[:2]
        self.assertEqual(event_name, 'survey_update_v2')
        self.assertEqual(event_payload["contract_version"], "surveys.live_results.v2")
        self.assertEqual(event_payload["result_version"], 42)
        self.assertNotIn("legacy_results", event_payload)
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'encuesta_consulta-barrial'})

    def test_emit_survey_update_emits_tenant_scoped_room_and_legacy_room(self):
        legacy_payload = {"total_respuestas": 1}
        modern_payload = {
            "contract_version": "surveys.live_results.v2",
            "result_version": 43,
            "tenant_slug": "junin",
            "total_respuestas": 1,
            "legacy_results": legacy_payload,
        }

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_survey_update("consulta-barrial", modern_payload, tenant_slug="junin")

        self.assertEqual(mock_emit.call_args_list[0], call('survey_update', legacy_payload, room='encuesta:junin:consulta-barrial'))
        self.assertEqual(mock_emit.call_args_list[1].args[0], 'survey_update_v2')
        self.assertEqual(mock_emit.call_args_list[1].kwargs, {'room': 'encuesta:junin:consulta-barrial'})
        self.assertEqual(mock_emit.call_args_list[2], call('survey_update', legacy_payload, room='encuesta_consulta-barrial'))
        self.assertEqual(mock_emit.call_args_list[3].args[0], 'survey_update_v2')
        self.assertEqual(mock_emit.call_args_list[3].kwargs, {'room': 'encuesta_consulta-barrial'})


if __name__ == '__main__':
    unittest.main()
