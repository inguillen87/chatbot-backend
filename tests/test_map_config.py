import unittest

from flask import Flask


class MapConfigTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()

    def tearDown(self) -> None:
        self.ctx.pop()

    def test_default_maptiler_style_appends_key(self) -> None:
        from utils import map_config

        self.app.config.update(
            GOOGLE_MAPS_API_KEY="",
            MAPTILER_API_KEY="demo-123",
            MAPLIBRE_STYLE_URL="",
            MAPTILER_STYLE_URL="",
        )

        config = map_config.get_map_config()

        self.assertEqual(config["provider"], "maptiler")
        self.assertIn("key=demo-123", config["style_url"])
        self.assertTrue(config["style_url"].endswith("key=demo-123"))

    def test_custom_style_supports_key_placeholder(self) -> None:
        from utils import map_config

        self.app.config.update(
            MAPTILER_API_KEY="abc123",
            MAPLIBRE_STYLE_URL="https://tiles.example/styles.json?token={key}",
        )

        config = map_config.get_map_config()

        self.assertEqual(
            config["style_url"],
            "https://tiles.example/styles.json?token=abc123",
        )

    def test_can_force_maptiler_provider(self) -> None:
        from utils import map_config

        self.app.config.update(
            GOOGLE_MAPS_API_KEY="should-not-be-used",
            MAPTILER_API_KEY="maptiler-demo",
            MAP_PROVIDER="maptiler",
        )

        config = map_config.get_map_config()

        self.assertEqual(config["provider"], "maptiler")
        self.assertEqual(config["maptiler_key"], "maptiler-demo")


if __name__ == "__main__":
    unittest.main()
