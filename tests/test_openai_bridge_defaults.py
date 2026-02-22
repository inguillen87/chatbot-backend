from types import SimpleNamespace

from services import openai_bridge


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15

    def to_dict(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _FakeCompletions:
    def create(self, **kwargs):
        content = '{"respuesta_usuario":"ok"}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=_FakeUsage(),
        )


class _FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=_FakeCompletions())


def test_llamar_openai_sets_required_default_keys(monkeypatch):
    monkeypatch.setattr(openai_bridge, "client", _FakeClient())

    parsed, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme"},
        historial=[],
        chat_session_id="session-1",
    )

    assert parsed["message_body"] == "ok"
    assert parsed["accion_backend"] == "responder_directamente"
    assert parsed["pedir_info"] is None
    assert parsed["datos_estructura"]["target"] == "pyme"
    assert parsed["botones"] == []
    assert meta["usage"]["total_tokens"] == 15
