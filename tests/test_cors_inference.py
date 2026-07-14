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

    def test_local_dev_origins_allow_dynamic_vite_ports(self):
        with patch.dict(os.environ, {"CORS_ALLOW_LOCAL_DEV": "1"}, clear=True):
            cfg = importlib.reload(app_config)
            origin = "http://127.0.0.1:4194"
            self.assertTrue(
                any(
                    getattr(allowed, "match", None) and allowed.match(origin)
                    for allowed in cfg.ALLOWED_ORIGINS
                ),
                "local Vite preview ports should be accepted by CORS in dev",
            )
        importlib.reload(app_config)

    def test_production_ignores_local_dev_flag_and_wildcard_previews(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://api.chatboc.ar",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "CORS_ALLOW_LOCAL_DEV": "1",
            "CORS_ALLOWED_ORIGINS": (
                "https://www.chatboc.ar,"
                "https://chatboc-production.vercel.app"
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertNotIn(
                "https://chatboc-production.vercel.app",
                cfg.CREDENTIALS_ALLOWED_ORIGINS,
            )
            self.assertIn("https://chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertIn("https://www.chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertFalse(
                any(
                    getattr(allowed, "match", None)
                    and allowed.match("http://localhost:5173")
                    for allowed in cfg.CREDENTIALS_ALLOWED_ORIGINS
                )
            )
            self.assertFalse(
                any(
                    getattr(allowed, "match", None)
                    and allowed.match("https://attacker.vercel.app")
                    for allowed in cfg.CREDENTIALS_ALLOWED_ORIGINS
                )
            )
        importlib.reload(app_config)

    def test_wildcard_is_never_a_credentialed_origin(self):
        env = {
            "ENV": "production",
            "BACKEND_URL": "https://api.chatboc.ar",
            "PUBLIC_ROOT_DOMAIN": "chatboc.ar",
            "CORS_ALLOWED_ORIGINS": "*",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = importlib.reload(app_config)
            self.assertNotIn("*", cfg.CREDENTIALS_ALLOWED_ORIGINS)
            self.assertIn("https://chatboc.ar", cfg.CREDENTIALS_ALLOWED_ORIGINS)
        importlib.reload(app_config)
