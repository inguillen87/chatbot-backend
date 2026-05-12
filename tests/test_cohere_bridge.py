from types import SimpleNamespace

from services import cohere_bridge


def test_cohere_chat_model_defaults_to_live_command_a(monkeypatch):
    monkeypatch.delenv("COHERE_CHAT_MODEL", raising=False)

    assert cohere_bridge._cohere_chat_model() == "command-a-03-2025"


def test_cohere_chat_model_can_be_overridden(monkeypatch):
    monkeypatch.setenv("COHERE_CHAT_MODEL", "command-r7b-12-2024")

    assert cohere_bridge._cohere_chat_model() == "command-r7b-12-2024"


def test_extract_response_text_supports_client_v2_shape():
    response = SimpleNamespace(
        message=SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text='{"respuesta_usuario":"Hola"}'),
            ]
        )
    )

    assert cohere_bridge._extract_response_text(response) == '{"respuesta_usuario":"Hola"}'


def test_parse_llm_json_normalizes_chatboc_contract():
    parsed = cohere_bridge._parse_llm_json('```json\n{"respuesta_usuario":"Hola"}\n```')

    assert parsed["message_body"] == "Hola"
    assert parsed["accion_backend"] == "responder_directamente"
    assert parsed["datos_estructura"] == {}
    assert parsed["botones"] == []
    assert parsed["pedir_info"] is None
