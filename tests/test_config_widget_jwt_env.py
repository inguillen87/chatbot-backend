import importlib
import os
import unittest
from unittest.mock import patch


class ConfigWidgetJwtEnvTestCase(unittest.TestCase):
    def _load_config_module(self):
        import config

        return importlib.reload(config)

    def test_widget_jwt_settings_are_loaded_from_env(self):
        with patch.dict(
            os.environ,
            {
                "WIDGET_JWT_ALG": "RS256",
                "WIDGET_JWT_KID": "widget-rs-main",
                "WIDGET_JWT_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----demo",
                "WIDGET_JWT_PUBLIC_KEY": "-----BEGIN PUBLIC KEY-----demo",
                "WIDGET_JWT_SECRET": "fallback-secret",
                "SECRET_KEY": "app-secret",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertEqual(str(cfg.Config.WIDGET_JWT_ALG).upper(), "RS256")
            self.assertEqual(cfg.Config.WIDGET_JWT_KID, "widget-rs-main")
            self.assertEqual(cfg.Config.WIDGET_JWT_PRIVATE_KEY, "-----BEGIN PRIVATE KEY-----demo")
            self.assertEqual(cfg.Config.WIDGET_JWT_PUBLIC_KEY, "-----BEGIN PUBLIC KEY-----demo")
            self.assertEqual(cfg.Config.WIDGET_JWT_SECRET, "fallback-secret")


if __name__ == "__main__":
    unittest.main()
