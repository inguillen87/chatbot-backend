import unittest
from types import SimpleNamespace

from routes.public_resolver import _quick_menu_for_widget


class PublicResolverQuickMenuTestCase(unittest.TestCase):
    def test_quick_menu_for_education_tenant(self):
        tenant = SimpleNamespace(
            tipo="pyme",
            pyme=SimpleNamespace(rubro=SimpleNamespace(nombre="Escuela Pública Técnica")),
            municipio=None,
        )
        menu = _quick_menu_for_widget(tenant)
        intents = [item.get("intent") for item in menu]
        self.assertIn("asistencia_alumno", intents)
        self.assertIn("tramites_secretaria", intents)
        self.assertEqual(menu[-1].get("institution_type"), "public")

    def test_quick_menu_for_municipio_keeps_civic_options(self):
        tenant = SimpleNamespace(tipo="municipio", pyme=None, municipio=SimpleNamespace(rubro=None))
        menu = _quick_menu_for_widget(tenant)
        intents = [item.get("intent") for item in menu]
        self.assertIn("iniciar_reclamo", intents)
        self.assertIn("analytics_heatmap", intents)


if __name__ == "__main__":
    unittest.main()
