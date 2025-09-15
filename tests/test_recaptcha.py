import unittest
from app import create_app
from utils.recaptcha import verify_recaptcha

class RecaptchaUtilsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_verify_recaptcha_without_secret_skips_check(self):
        # Ensure no secret configured
        self.app.config.pop("RECAPTCHA_SECRET_KEY", None)
        # Should bypass verification and return True
        self.assertTrue(verify_recaptcha("dummy"))

if __name__ == "__main__":
    unittest.main()
