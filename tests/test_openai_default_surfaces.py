from unittest.mock import patch

from services import llm_utils
from services.municipio_responder import _get_reclamos_menu, handle_main_menu_action
from services.openai_model_defaults import (
    DEFAULT_OPENAI_TERRA_MODEL,
    DEFAULT_OPENAI_TTS_MODEL,
)


def test_structured_json_uses_terra_role_default(monkeypatch):
    monkeypatch.delenv("OPENAI_CHAT_MODEL_EXTRACTION", raising=False)

    with patch(
        "services.llm_bridge.llamar_llm_para_generacion_texto",
        return_value='{"categoria": "Luminaria"}',
    ) as call:
        result = llm_utils.llamar_llm_para_json_estructurado("system", "user")

    assert result == {"categoria": "Luminaria"}
    assert call.call_args.kwargs["model"] == DEFAULT_OPENAI_TERRA_MODEL


def test_structured_json_preserves_environment_and_explicit_overrides(monkeypatch):
    monkeypatch.setenv("OPENAI_CHAT_MODEL_EXTRACTION", "tenant-extraction-model")

    with patch(
        "services.llm_bridge.llamar_llm_para_generacion_texto",
        return_value="{}",
    ) as call:
        llm_utils.llamar_llm_para_json_estructurado("system", "user")
        assert call.call_args.kwargs["model"] == "tenant-extraction-model"

        llm_utils.llamar_llm_para_json_estructurado(
            "system",
            "user",
            model="explicit-model",
        )
        assert call.call_args.kwargs["model"] == "explicit-model"


def test_reclamos_menu_uses_current_tts_default(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_MENU_MODEL", raising=False)

    response = _get_reclamos_menu({})

    assert response["tts_model"] == DEFAULT_OPENAI_TTS_MODEL


def test_reclamos_menu_preserves_tts_override(monkeypatch):
    monkeypatch.setenv("OPENAI_TTS_MENU_MODEL", "tenant-tts-model")

    response = _get_reclamos_menu({})

    assert response["tts_model"] == "tenant-tts-model"


def test_video_and_portal_audio_use_current_tts_default(monkeypatch):
    monkeypatch.delenv("OPENAI_TTS_MENU_MODEL", raising=False)

    with patch("services.municipio_responder._track_whatsapp_conversion_event"):
        video = handle_main_menu_action("solicitar_videollamada_ia", {}, None)
        portal = handle_main_menu_action("mi_portal_usuario", {}, None)

    assert video["tts_model"] == DEFAULT_OPENAI_TTS_MODEL
    assert portal["tts_model"] == DEFAULT_OPENAI_TTS_MODEL
