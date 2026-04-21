import unittest
from unittest.mock import patch

from flask import Flask

from routes.rubros import rubros_bp


class _EmptyQuery:
    def filter_by(self, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return []


class _FakeRubroModel:
    class _NombreColumn:
        @staticmethod
        def asc():
            return None

    nombre = _NombreColumn()
    query = _EmptyQuery()


class RubrosDemoModeTestCase(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["ENABLE_DEMO_MODE"] = False
        self.app.register_blueprint(rubros_bp, url_prefix="/rubros")
        self.client = self.app.test_client()

    def test_rubros_does_not_inject_demo_entries_when_demo_mode_disabled(self):
        with patch("routes.rubros.Rubro", _FakeRubroModel), patch(
            "routes.rubros.load_demo_rubros",
            side_effect=AssertionError("load_demo_rubros should not be called when demo mode is disabled"),
        ):
            response = self.client.get("/rubros/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), [])


if __name__ == "__main__":
    unittest.main()
