import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from routes.omnichannel import omnichannel_bp


class OmnichannelRoutesTest(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.register_blueprint(omnichannel_bp)
        self.client = app.test_client()

    def test_resolve_conversation_endpoint(self):
        with patch('routes.omnichannel.resolve_or_create_conversation', return_value=SimpleNamespace(to_dict=lambda: {'id': 'conv-1', 'status': 'open'})):
            response = self.client.post('/api/conversations/resolve', json={'channel': 'whatsapp', 'tenant_id': 3})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['conversation']['id'], 'conv-1')

    def test_conversation_timeline_endpoint(self):
        timeline = {
            'conversation': {'id': 'conv-1', 'status': 'open'},
            'channel_sessions': [{'id': 'sess-1', 'channel': 'whatsapp'}],
            'messages': [{'id': 'msg-1', 'direction': 'inbound'}],
        }
        with patch('routes.omnichannel.get_conversation_timeline', return_value=timeline):
            response = self.client.get('/api/conversations/conv-1/timeline')

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload['conversation']['id'], 'conv-1')
        self.assertEqual(payload['messages'][0]['id'], 'msg-1')


if __name__ == '__main__':
    unittest.main()
