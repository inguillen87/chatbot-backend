import unittest
from unittest.mock import patch, call

from socket_service import emit_new_ticket, emit_ticket_comment, emit_ticket_status_changed


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

        mock_emit.assert_has_calls(
            [
                call('new_comment', payload, room='pyme_3'),
                call('conversation.message.created', payload, room='pyme_3'),
            ]
        )

    def test_emit_ticket_status_changed_emits_legacy_and_standard_events(self):
        payload = {"socket_room": "municipio_7", "tenant_type": "municipio", "ticket_id": 11, "estado": "en_proceso"}

        with patch('socket_service.socketio.emit') as mock_emit:
            emit_ticket_status_changed(payload)

        mock_emit.assert_has_calls(
            [
                call('ticket.status.changed', payload, room='municipio_7'),
                call('ticket_update', payload, room='municipio_7'),
            ]
        )


if __name__ == '__main__':
    unittest.main()
