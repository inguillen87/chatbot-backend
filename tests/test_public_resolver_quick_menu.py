import unittest
from types import SimpleNamespace

from routes.public_resolver import _quick_menu_for_widget, _widget_fixed_menu_audio_contract


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
        self.assertIn("justificar_inasistencia", intents)
        self.assertIn("convivencia_escolar", intents)
        self.assertIn("tramites_secretaria", intents)
        self.assertEqual(menu[-1].get("institution_type"), "public")

    def test_quick_menu_for_municipio_keeps_civic_options(self):
        tenant = SimpleNamespace(tipo="municipio", pyme=None, municipio=SimpleNamespace(rubro=None))
        menu = _quick_menu_for_widget(tenant)
        intents = [item.get("intent") for item in menu]
        self.assertIn("iniciar_reclamo", intents)
        self.assertIn("analytics_heatmap", intents)

    def test_widget_fixed_menu_audio_contract_is_tenant_scoped_and_cacheable(self):
        tenant = SimpleNamespace(
            slug="junin",
            nombre="Municipalidad de Junin",
            tipo="municipio",
        )
        quick_menu = [
            {"id": "menu_reclamo", "label": "Crear reclamo", "intent": "iniciar_reclamo"},
            {"id": "menu_estado", "label": "Estado ticket", "intent": "consultar_ticket"},
        ]

        contract = _widget_fixed_menu_audio_contract(tenant, quick_menu)

        self.assertTrue(contract["enabled"])
        self.assertEqual(
            contract["tts_cache_namespace"],
            "whatsapp:menu:junin:quick-menu:widget:full:v1",
        )
        self.assertEqual(contract["audio_cache_policy"]["kind"], "fixed_menu")
        self.assertEqual(contract["tts_cache_text"], contract["audio_text"])
        self.assertIn("Opcion 1, Crear reclamo.", contract["tts_cache_text"])
        self.assertNotIn("example.com", contract["tts_cache_namespace"])
        self.assertNotIn("549", contract["tts_cache_namespace"])


if __name__ == "__main__":
    unittest.main()
