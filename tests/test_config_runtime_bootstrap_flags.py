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
                "ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "ENABLE_RUNTIME_TENANT_INIT": "",
                "FLASK_ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "FLASK_ENABLE_RUNTIME_TENANT_INIT": "",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertFalse(cfg.Config.ENABLE_RUNTIME_SCHEMA_SYNC)
            self.assertFalse(cfg.Config.ENABLE_RUNTIME_TENANT_INIT)

    def test_runtime_bootstrap_defaults_enabled_on_local_dev(self):
        with patch.dict(
            os.environ,
            {
                "ENV": "dev",
                "RENDER": "false",
                "RENDER_EXTERNAL_URL": "",
                "ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "ENABLE_RUNTIME_TENANT_INIT": "",
                "FLASK_ENABLE_RUNTIME_SCHEMA_SYNC": "",
                "FLASK_ENABLE_RUNTIME_TENANT_INIT": "",
            },
            clear=False,
        ):
            cfg = self._load_config_module()
            self.assertTrue(cfg.Config.ENABLE_RUNTIME_SCHEMA_SYNC)
            self.assertTrue(cfg.Config.ENABLE_RUNTIME_TENANT_INIT)


if __name__ == "__main__":
    unittest.main()
