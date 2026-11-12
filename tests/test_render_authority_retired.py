"""Vercel runtime cannot regain legacy Render secret/deployment authority."""
import os
import unittest
from unittest.mock import patch

from services.render_env_sync import sync_render_env_var


class RetiredRenderAuthorityTests(unittest.TestCase):
    def test_process_runtime_wins_over_caller_config(self):
        config = {"RENDER_ENV_SYNC_ENABLED": True,
                  "RENDER_ENV_SYNC_TRIGGER_DEPLOY_ENABLED": True,
                  "RENDER_SERVICE_ID": "synthetic-service", "RENDER_API_KEY": "synthetic-key"}
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=True), \
             patch("services.render_env_sync.requests.put") as put, \
             patch("services.render_env_sync.requests.post") as post:
            result = sync_render_env_var("PROVIDER_TOKEN", "synthetic-secret", config)
        put.assert_not_called()
        post.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertFalse(result["secret_value_stored"])
        self.assertFalse(result["deploy"]["triggered"])
        self.assertEqual(result["reason_code"], "render_authority_retired_for_vercel")
        self.assertNotIn("synthetic-secret", str(result))

    def test_config_runtime_blocks_even_without_process_signals(self):
        for signal in ({"VERCEL_ENV": "production"}, {"VERCEL_ENV": "preview"},
                       {"VERCEL_URL": "synthetic.vercel.app"}):
            with self.subTest(signal=signal), patch.dict(os.environ, {}, clear=True), \
                 patch("services.render_env_sync.requests.put") as put:
                result = sync_render_env_var("PROVIDER_TOKEN", "synthetic-secret",
                                             {**signal, "RENDER_ENV_SYNC_ENABLED": True})
            put.assert_not_called()
            self.assertEqual(result["reason_code"], "render_authority_retired_for_vercel")


if __name__ == '__main__':
    unittest.main()
