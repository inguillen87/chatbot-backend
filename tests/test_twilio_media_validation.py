import unittest
from flask import Flask

from routes.whatsapp_webhook import _is_valid_media_url, _prepare_media_param

class TestTwilioMediaValidation(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['BASE_URL'] = 'https://api.example.com'
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_is_valid_media_url(self):
        self.assertTrue(_is_valid_media_url('https://example.com/file.png'))
        self.assertTrue(_is_valid_media_url('https://example.com/file.mp3'))
        self.assertFalse(_is_valid_media_url('http://example.com/file.png'))
        self.assertFalse(_is_valid_media_url('https://example.com/file.svg'))

    def test_prepare_media_param(self):
        params = {'from_': 'a', 'to': 'b', 'media_url': ['/static/file.svg']}
        result = _prepare_media_param(params)
        self.assertNotIn('media_url', result)

        params = {'from_': 'a', 'to': 'b', 'media_url': ['/static/file.png']}
        result = _prepare_media_param(params)
        self.assertEqual(result['media_url'][0], 'https://api.example.com/static/file.png')

if __name__ == '__main__':
    unittest.main()
