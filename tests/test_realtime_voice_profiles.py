import unittest
from types import SimpleNamespace

from services.realtime_voice_profiles import (
    DEFAULT_REALTIME_INPUT_TRANSCRIPTION_MODEL,
    DEFAULT_REALTIME_TRANSCRIPTION_MODEL,
    DEFAULT_REALTIME_VOICE_MODEL,
    REALTIME_VOICE_CONTRACT_VERSION,
    build_multilingual_translation_policy,
    build_realtime_voice_capabilities,
    build_realtime_voice_instructions,
    build_realtime_voice_tools,
    infer_realtime_voice_vertical,
    resolve_realtime_input_transcription_model,
    resolve_realtime_model,
    resolve_realtime_transcription_model,
)


class RealtimeVoiceProfilesTestCase(unittest.TestCase):
    def test_defaults_to_current_realtime_model(self):
        capabilities = build_realtime_voice_capabilities()

        self.assertEqual(capabilities["contract_version"], REALTIME_VOICE_CONTRACT_VERSION)
        self.assertEqual(capabilities["recommended_model"], DEFAULT_REALTIME_VOICE_MODEL)
        self.assertEqual(capabilities["fallback_model"], "gpt-realtime-2.1")
        self.assertEqual(
            capabilities["live_transcription"]["model"],
            DEFAULT_REALTIME_TRANSCRIPTION_MODEL,
        )
        self.assertEqual(
            capabilities["live_transcription"]["session_type"],
            "transcription",
        )
        self.assertEqual(
            capabilities["input_transcription"]["model"],
            DEFAULT_REALTIME_INPUT_TRANSCRIPTION_MODEL,
        )
        self.assertTrue(capabilities["native_speech_to_speech"])
        self.assertEqual(capabilities["transports"]["phone_primary"], "openai_realtime_sip")
        self.assertEqual(capabilities["transports"]["phone_bridge"], "twilio_media_streams")
        self.assertEqual(capabilities["cost_latency_policy"]["external_tts"], "fallback_only")
        translation = capabilities["translation"]
        self.assertTrue(translation["enabled"])
        self.assertEqual(translation["target_language"], "es")
        self.assertEqual(
            {language["code"] for language in translation["supported_languages"]},
            {"es", "en", "pt"},
        )
        self.assertTrue(translation["channels"]["realtime_voice_call"])
        self.assertTrue(translation["channels"]["admin_transcript"])
        self.assertEqual(translation["dedicated_realtime_translation"]["model"], "gpt-realtime-translate")

    def test_translation_policy_can_be_tenant_configured(self):
        policy = build_multilingual_translation_policy(
            {
                "translation_enabled": False,
                "translation_supported_languages": "es,pt,fr",
                "translation_target_language": "pt",
            }
        )

        self.assertFalse(policy["enabled"])
        self.assertEqual(policy["target_language"], "pt")
        self.assertEqual([language["code"] for language in policy["supported_languages"]], ["es", "pt"])

    def test_realtime_instructions_include_multilingual_rules(self):
        instructions = build_realtime_voice_instructions(
            tenant_name="Municipio Inteligente",
            vertical="municipio",
            translation_policy=build_multilingual_translation_policy(),
        )

        self.assertIn("ingles", instructions)
        self.assertIn("portugues", instructions)
        self.assertIn("panel admin", instructions)
        self.assertIn("Conserva nombres propios", instructions)

    def test_config_can_override_model_without_losing_fallback(self):
        model = resolve_realtime_model({"openai_realtime_model": "gpt-realtime-custom"})

        self.assertEqual(model, "gpt-realtime-custom")

    def test_live_transcription_model_is_configurable(self):
        model = resolve_realtime_transcription_model(
            {"openai_live_transcription_model": "gpt-live-transcribe-custom"}
        )

        self.assertEqual(model, "gpt-live-transcribe-custom")

    def test_realtime_call_input_transcription_is_configured_independently(self):
        model = resolve_realtime_input_transcription_model(
            {"openai_realtime_input_transcription_model": "gpt-realtime-whisper"}
        )

        self.assertEqual(model, "gpt-realtime-whisper")

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
        self.assertIn("consultar_caso_escolar", tools)
        self.assertIn("registrar_solicitud_operativa", tools)
        self.assertIn("inasistencias", instructions)

    def test_municipio_and_pyme_keep_different_actions(self):
        municipio_tools = [tool["name"] for tool in build_realtime_voice_tools("municipio")]
        pyme_tools = [tool["name"] for tool in build_realtime_voice_tools("pyme")]

        self.assertIn("crear_reclamo", municipio_tools)
        self.assertIn("consultar_estado_reclamo", municipio_tools)
        self.assertIn("consultar_tramite", municipio_tools)
        self.assertIn("registrar_solicitud_operativa", municipio_tools)
        self.assertNotIn("crear_pedido", municipio_tools)
        self.assertIn("crear_pedido", pyme_tools)
        self.assertIn("consultar_producto", pyme_tools)
        self.assertIn("consultar_estado_pedido", pyme_tools)
        self.assertIn("registrar_solicitud_operativa", pyme_tools)

    def test_general_vertical_can_register_operational_requests_for_any_rubro(self):
        capabilities = build_realtime_voice_capabilities(SimpleNamespace(tipo="ong", nombre="ONG Demo"))
        general_tools = [tool["name"] for tool in build_realtime_voice_tools("general")]

        self.assertEqual(capabilities["active_vertical"], "general")
        self.assertIn("general", capabilities["verticals"])
        self.assertIn("registrar_solicitud_operativa", general_tools)
        self.assertIn("capturar_lead_comercial", general_tools)
        self.assertIn("registrar_solicitud_operativa", capabilities["verticals"]["general"]["actions"])


if __name__ == "__main__":
    unittest.main()
