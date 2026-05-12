import unittest
from types import SimpleNamespace

from services.realtime_voice_profiles import (
    DEFAULT_REALTIME_VOICE_MODEL,
    REALTIME_VOICE_CONTRACT_VERSION,
    build_realtime_voice_capabilities,
    build_realtime_voice_instructions,
    build_realtime_voice_tools,
    infer_realtime_voice_vertical,
    resolve_realtime_model,
)


class RealtimeVoiceProfilesTestCase(unittest.TestCase):
    def test_defaults_to_current_realtime_model(self):
        capabilities = build_realtime_voice_capabilities()

        self.assertEqual(capabilities["contract_version"], REALTIME_VOICE_CONTRACT_VERSION)
        self.assertEqual(capabilities["recommended_model"], DEFAULT_REALTIME_VOICE_MODEL)
        self.assertEqual(capabilities["fallback_model"], "gpt-realtime")
        self.assertTrue(capabilities["native_speech_to_speech"])

    def test_config_can_override_model_without_losing_fallback(self):
        model = resolve_realtime_model({"openai_realtime_model": "gpt-realtime-custom"})

        self.assertEqual(model, "gpt-realtime-custom")

    def test_school_tenant_gets_school_case_tool(self):
        tenant = SimpleNamespace(
            tipo="pyme",
            nombre="Colegio San Martin",
            vertical="educacion",
            subvertical="colegio",
            capabilities_json={"education": {"enabled": True}},
            pyme=SimpleNamespace(rubro=SimpleNamespace(nombre="Colegio privado")),
            municipio=None,
        )

        vertical = infer_realtime_voice_vertical(tenant)
        tools = [tool["name"] for tool in build_realtime_voice_tools(vertical)]
        instructions = build_realtime_voice_instructions(
            tenant_name=tenant.nombre,
            vertical=vertical,
            user_name="Ana",
        )

        self.assertEqual(vertical, "colegio")
        self.assertIn("crear_caso_escolar", tools)
        self.assertIn("inasistencias", instructions)

    def test_municipio_and_pyme_keep_different_actions(self):
        municipio_tools = [tool["name"] for tool in build_realtime_voice_tools("municipio")]
        pyme_tools = [tool["name"] for tool in build_realtime_voice_tools("pyme")]

        self.assertIn("crear_reclamo", municipio_tools)
        self.assertNotIn("crear_pedido", municipio_tools)
        self.assertIn("crear_pedido", pyme_tools)
        self.assertIn("consultar_producto", pyme_tools)


if __name__ == "__main__":
    unittest.main()
