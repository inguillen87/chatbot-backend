import os
import unittest
from unittest.mock import patch

from services.actions import municipio_actions


class MunicipioActionsEnvParsingTests(unittest.TestCase):
    def test_parse_int_env_uses_value_when_valid(self):
        with patch.dict(os.environ, {"CHATBOC_RECLAMO_DEDUP_WINDOW_SECONDS": "120"}, clear=False):
            self.assertEqual(
                municipio_actions._parse_int_env("CHATBOC_RECLAMO_DEDUP_WINDOW_SECONDS", 600),
                120,
            )

    def test_parse_int_env_falls_back_when_invalid(self):
        with patch.dict(os.environ, {"CHATBOC_RECLAMO_DEDUP_WINDOW_SECONDS": "abc"}, clear=False):
            self.assertEqual(
                municipio_actions._parse_int_env("CHATBOC_RECLAMO_DEDUP_WINDOW_SECONDS", 600),
                600,
            )


if __name__ == "__main__":
    unittest.main()
