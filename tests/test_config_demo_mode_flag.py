import importlib
import os
import unittest
from unittest.mock import patch


class ConfigDemoModeFlagTestCase(unittest.TestCase):
    def _load_config_module(self):
        import config

        return importlib.reload(config)

    def test_demo_mode_disabled_by_default(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_DEMO_MODE": "",
                "FLASK_ENABLE_DEMO_MODE": "",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertFalse(cfg.Config.ENABLE_DEMO_MODE)

    def test_demo_mode_can_be_enabled_explicitly(self):
        with patch.dict(
            os.environ,
            {
                "ENABLE_DEMO_MODE": "true",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertTrue(cfg.Config.ENABLE_DEMO_MODE)

    def test_demo_welcome_message_does_not_reopen_legacy_rubro_selector(self):
        with patch.dict(os.environ, {"DEMO_WELCOME_MESSAGE": ""}, clear=False):
            cfg = self._load_config_module()
            message = cfg.Config.DEMO_WELCOME_MESSAGE.lower()
            self.assertNotIn("showroom interactivo", message)
            self.assertNotIn("elegi el rubro", message)
            self.assertNotIn("elegí el rubro", message)


if __name__ == "__main__":
    unittest.main()
