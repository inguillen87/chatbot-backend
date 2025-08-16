import os
import importlib
from unittest import TestCase
from unittest.mock import patch
import config as app_config

class TestCorsInference(TestCase):
    def test_backend_root_domain_added(self):
        env = {
            "BACKEND_URL": "https://api.example.com",
            "ENV": "prod"
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn("https://example.com", cfg.ALLOWED_ORIGINS)
            self.assertIn("https://www.example.com", cfg.ALLOWED_ORIGINS)
        importlib.reload(app_config)

    def test_public_root_domain_env(self):
        env = {
            "PUBLIC_ROOT_DOMAIN": "example.org"
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertIn("https://example.org", cfg.ALLOWED_ORIGINS)
            self.assertIn("https://www.example.org", cfg.ALLOWED_ORIGINS)
        importlib.reload(app_config)
