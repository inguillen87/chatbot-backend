import importlib
import os
import unittest
from unittest.mock import patch


class ConfigRuntimeBootstrapFlagsTestCase(unittest.TestCase):
    def _load_config_module(self):
        import config

        return importlib.reload(config)

    def test_runtime_bootstrap_defaults_disabled_on_render_even_if_env_dev(self):
        with patch.dict(
            os.environ,
            {
                "ENV": "dev",
                "RENDER": "true",
                "RENDER_EXTERNAL_URL": "https://chatboc-backend.onrender.com",
                "COOKIE_DOMAIN": "",
                "ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "ENABLE_RUNTIME_TENANT_INIT": "",
                "FLASK_ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "FLASK_ENABLE_RUNTIME_TENANT_INIT": "",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertEqual(cfg.Config.ENV, "prod")
            self.assertTrue(cfg.IS_PRODUCTION_RUNTIME)
            self.assertFalse(cfg.Config.DEBUG)
            self.assertIsNone(cfg.Config.SESSION_COOKIE_DOMAIN)
            self.assertFalse(cfg.Config.ENABLE_RUNTIME_SCHEMA_SYNC)
            self.assertFalse(cfg.Config.ENABLE_RUNTIME_TENANT_INIT)

    def test_render_is_production_when_env_is_absent(self):
        with patch.dict(
            os.environ,
            {
                "RENDER": "true",
                "RENDER_EXTERNAL_URL": "https://chatboc-backend.onrender.com",
                "FLASK_ENV": "",
                "COOKIE_DOMAIN": "",
            },
            clear=False,
        ):
            os.environ.pop("ENV", None)
            cfg = self._load_config_module()

            self.assertEqual(cfg.Config.ENV, "prod")
            self.assertTrue(cfg.IS_PRODUCTION_RUNTIME)
            self.assertFalse(cfg.Config.DEBUG)
            self.assertIsNone(cfg.Config.SESSION_COOKIE_DOMAIN)

    def test_flask_env_production_is_fail_closed_without_env_or_render(self):
        with patch.dict(
            os.environ,
            {
                "FLASK_ENV": "production",
                "RENDER": "false",
                "RENDER_EXTERNAL_URL": "",
                "COOKIE_DOMAIN": "",
            },
            clear=False,
        ):
            os.environ.pop("ENV", None)
            cfg = self._load_config_module()

            self.assertEqual(cfg.Config.ENV, "prod")
            self.assertTrue(cfg.IS_PRODUCTION_RUNTIME)
            self.assertFalse(cfg.Config.DEBUG)
            self.assertIsNone(cfg.Config.SESSION_COOKIE_DOMAIN)

    def test_explicit_cookie_domain_override_is_preserved(self):
        with patch.dict(
            os.environ,
            {
                "ENV": "production",
                "FLASK_ENV": "",
                "RENDER": "false",
                "RENDER_EXTERNAL_URL": "",
                "COOKIE_DOMAIN": ".chatboc.ar",
            },
            clear=False,
        ):
            cfg = self._load_config_module()

            self.assertEqual(cfg.Config.ENV, "prod")
            self.assertFalse(cfg.Config.DEBUG)
            self.assertEqual(cfg.Config.SESSION_COOKIE_DOMAIN, ".chatboc.ar")

    def test_runtime_bootstrap_defaults_enabled_on_local_dev(self):
        with patch.dict(
            os.environ,
            {
                "ENV": "dev",
                "RENDER": "false",
                "RENDER_EXTERNAL_URL": "",
                "COOKIE_DOMAIN": "",
                "ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "ENABLE_RUNTIME_TENANT_INIT": "",
                "FLASK_ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "FLASK_ENABLE_RUNTIME_TENANT_INIT": "",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertEqual(cfg.Config.ENV, "dev")
            self.assertFalse(cfg.IS_PRODUCTION_RUNTIME)
            self.assertTrue(cfg.Config.DEBUG)
            self.assertTrue(cfg.Config.ENABLE_RUNTIME_SCHEMA_SYNC)
            self.assertTrue(cfg.Config.ENABLE_RUNTIME_TENANT_INIT)


if __name__ == "__main__":
    unittest.main()
