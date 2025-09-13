import unittest
import os
import base64

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

if __name__ == '__main__':
    unittest.main()
