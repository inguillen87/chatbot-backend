import unittest
import os
import base64
from unittest.mock import patch

from services.multimodal_analyzer import encode_image_to_base64
from app import app

class TestMultimodalAnalyzer(unittest.TestCase):
    def setUp(self):
        self.ctx = app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_encode_image_local_path(self):
        test_path = os.path.join(app.root_path, 'static/uploads/test_local_img.txt')
        os.makedirs(os.path.dirname(test_path), exist_ok=True)
        with open(test_path, 'wb') as f:
            f.write(b'hello')
        try:
            b64 = encode_image_to_base64('/static/uploads/test_local_img.txt')
            self.assertEqual(base64.b64decode(b64), b'hello')
        finally:
            os.remove(test_path)

    @patch('services.multimodal_analyzer.requests.get')
    @patch('os.path.exists', return_value=False)
    def test_encode_image_fallback_to_base_url(self, mock_exists, mock_get):
        app.config['APP_PUBLIC_BASE_URL'] = 'https://cdn.example.com'
        mock_get.return_value = type('resp', (), {'content': b'data', 'raise_for_status': lambda self=None: None})()
        b64 = encode_image_to_base64('/static/uploads/remote_img.jpg')
        mock_get.assert_called_with('https://cdn.example.com/static/uploads/remote_img.jpg')
        self.assertEqual(base64.b64decode(b64), b'data')

if __name__ == '__main__':
    unittest.main()
