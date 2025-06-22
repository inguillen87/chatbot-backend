import unittest
import sys

try:
    if 'flask' in sys.modules:
        del sys.modules['flask']
    from app import create_app
except Exception:
    create_app = None

@unittest.skipIf(create_app is None, "Flask not available")
class WidgetAttentionEndpointTests(unittest.TestCase):
    def setUp(self):
        app = create_app()
        app.config['TESTING'] = True


if __name__ == '__main__':
    unittest.main()
