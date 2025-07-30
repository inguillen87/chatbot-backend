import unittest
from unittest.mock import patch, MagicMock

import services.notifications as notifications

class TestSendWhatsappTemplate(unittest.TestCase):
    @patch('services.notifications.Client')
    @patch.dict(os.environ, {'TWILIO_ACCOUNT_SID': 'sid', 'TWILIO_AUTH_TOKEN': 'token'})
    def test_enviar_bienvenida_whatsapp(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_message = MagicMock()
        mock_client.messages.create.return_value = mock_message

        notifications.enviar_bienvenida_whatsapp('+5491111111111', 'Marce')

        mock_client.messages.create.assert_called_once()
        args, kwargs = mock_client.messages.create.call_args
        assert kwargs['to'] == 'whatsapp:+5491111111111'
        assert kwargs['from_'] == notifications.TWILIO_WHATSAPP_NUMBER
        assert 'template' in kwargs

if __name__ == '__main__':
    unittest.main()
