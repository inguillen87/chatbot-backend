import unittest
from utils.session_utils import get_global_session_id

class TestSessionUtils(unittest.TestCase):
    def test_phone_session(self):
        self.assertEqual(get_global_session_id(phone="123"), "global_123")

    def test_email_session(self):
        self.assertEqual(get_global_session_id(email="a@b.com"), "global_a@b.com")

    def test_random_session(self):
        sid = get_global_session_id()
        self.assertTrue(sid.startswith("anon_"))

if __name__ == '__main__':
    unittest.main()
