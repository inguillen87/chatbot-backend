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
    def __init__(self):
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        content = '{"respuesta_usuario":"ok"}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=_FakeUsage(),
        )


class _FakeClient:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


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
    assert meta["model_used"] == "gpt-4o-mini"



def test_llamar_openai_uses_channel_specific_model(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(openai_bridge, "client", fake_client)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WHATSAPP", "gpt-5-mini")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme", "channel": "whatsapp"},
        historial=[],
        chat_session_id="session-2",
    )

    assert fake_client.completions.last_kwargs["model"] == "gpt-5-mini"
    assert meta["model_used"] == "gpt-5-mini"


def test_llamar_openai_uses_high_complexity_model(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(openai_bridge, "client", fake_client)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WIDGET", "gpt-4o-mini")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_HIGH_COMPLEXITY", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_CHAT_COMPLEXITY_MIN_CHARS", "20")
    monkeypatch.setenv("OPENAI_CHAT_COMPLEXITY_MIN_TURNS", "10")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario='{"texto": "Necesito una respuesta bastante extensa para un caso complejo", "channel": "widget"}',
        usuario={"tipo_entidad": "pyme"},
        historial=[],
        chat_session_id="session-3",
    )

    assert fake_client.completions.last_kwargs["model"] == "gpt-5-mini"
    assert meta["model_used"] == "gpt-5-mini"


def test_llamar_openai_explicit_model_has_priority(monkeypatch):
    fake_client = _FakeClient()
    monkeypatch.setattr(openai_bridge, "client", fake_client)
    monkeypatch.setenv("OPENAI_CHAT_MODEL_DEFAULT", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_CHAT_MODEL_WHATSAPP", "gpt-5")

    _, meta = openai_bridge.llamar_openai(
        app=None,
        mensaje_usuario="hola",
        usuario={"tipo_entidad": "pyme", "channel": "whatsapp"},
        historial=[],
        chat_session_id="session-4",
        model="gpt-4.1-mini",
    )

    assert fake_client.completions.last_kwargs["model"] == "gpt-4.1-mini"
    assert meta["model_used"] == "gpt-4.1-mini"
