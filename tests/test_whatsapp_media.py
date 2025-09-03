import unittest
from unittest.mock import patch, MagicMock
import utils.whatsapp as whatsapp


class TestWhatsappMedia(unittest.TestCase):
    @patch('utils.whatsapp.Client')
    def test_enviar_imagen_whatsapp(self, mock_client_cls):
        whatsapp.TWILIO_ACCOUNT_SID = 'sid'
        whatsapp.TWILIO_AUTH_TOKEN = 'token'
        whatsapp.TWILIO_WHATSAPP_NUMBER = '12345'
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_message = MagicMock()
        mock_client.messages.create.return_value = mock_message

        result = whatsapp.enviar_imagen_whatsapp('+5491111111111', 'Hola', 'http://test/img.jpg')

        assert result is True
        mock_client.messages.create.assert_called_once()
        args, kwargs = mock_client.messages.create.call_args
        assert kwargs['to'] == 'whatsapp:+5491111111111'
        assert kwargs['from_'] == f'whatsapp:{whatsapp.TWILIO_WHATSAPP_NUMBER}'
        assert kwargs['media_url'] == ['http://test/img.jpg']


if __name__ == '__main__':
    unittest.main()
