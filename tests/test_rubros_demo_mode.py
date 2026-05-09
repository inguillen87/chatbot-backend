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

    def test_rubros_tree_in_demo_mode_includes_three_pillars_and_colegios(self):
        self.app.config["ENABLE_DEMO_MODE"] = True
        with patch("routes.rubros.Rubro", _FakeRubroModel), patch("routes.rubros.load_demo_rubros", return_value=[]):
            response = self.client.get("/rubros/?format=tree")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        root_names = {item.get("nombre") for item in payload}
        self.assertIn("Soluciones para Empresas", root_names)
        self.assertIn("Soluciones para Sector Publico", root_names)
        self.assertIn("Colegios e instituciones educativas", root_names)
        education_root = next(item for item in payload if item.get("id") == 3)
        child_keys = {item.get("clave") for item in education_root.get("children") or []}
        self.assertIn("colegios", child_keys)


if __name__ == "__main__":
    unittest.main()
