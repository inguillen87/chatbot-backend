import unittest
from app import create_app, db
from config import TestConfig

class TempTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app(TestConfig)
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_temp(self):
        self.assertTrue(True)

if __name__ == '__main__':
    unittest.main()
