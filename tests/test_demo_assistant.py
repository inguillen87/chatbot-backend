"""A typed question the script does not cover is answered by a low-cost model, safely."""

from unittest.mock import patch

import pytest

from services import demo_assistant
from services.demo_response_engine import maybe_handle_demo_interaction

META = {"key": "bodega", "nombre": "Cuatro Fincas", "resources": []}


def ask(text, **kwargs):
    return maybe_handle_demo_interaction(pregunta=text, tipo_chat="pyme", demo_metadata=META, **kwargs)


@pytest.fixture(autouse=True)
def fresh_budget(monkeypatch):
    demo_assistant.reset_budget_for_tests()
    monkeypatch.setenv("DEMO_LLM_PROVIDERS", "gemini")
    monkeypatch.setattr(demo_assistant, "_network_allowed", lambda provider: True)


def test_an_uncovered_question_gets_the_model_answer_grounded_in_the_business():
    seen = {}

    def fake(system_prompt, question, timeout):
        seen["prompt"], seen["question"] = system_prompt, question
        return "Sí, el Malbec va muy bien con carnes asadas."

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": fake}):
        answer = ask("¿con qué comida me recomendás el vino tinto?")

    assert answer["message_body"] == "Sí, el Malbec va muy bien con carnes asadas."
    assert answer["options_list"], "the visitor still has a way back to the menu"
    assert "Cuatro Fincas" in seen["prompt"]
    assert "Malbec Reserva" in seen["prompt"], "the model only sees what the demo business publishes"
    assert "Nunca inventes precios" in seen["prompt"]


def test_a_product_question_never_reaches_the_model():
    def boom(*args):
        raise AssertionError("the catalogue answers this for free")

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": boom}):
        assert "$ 18.500" in ask("¿cuánto sale el malbec?")["message_body"]


def test_a_button_never_reaches_the_model():
    def boom(*args):
        raise AssertionError("buttons are scripted")

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": boom}):
        answer = ask({"action": "demo_action:bodega:welcome"}, action_id="demo_action:bodega:welcome")
    assert "Cuatro Fincas" in answer["message_body"]


def test_a_failing_provider_falls_back_to_the_script():
    def down(*args):
        raise TimeoutError("provider down")

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": down}):
        answer = ask("¿con qué comida me recomendás el vino tinto?")
    assert "menú" in answer["message_body"]


def test_the_hourly_ceiling_stops_calls(monkeypatch):
    monkeypatch.setenv("DEMO_LLM_MAX_PER_HOUR", "1")
    calls = []

    def fake(system_prompt, question, timeout):
        calls.append(question)
        return "Respuesta de prueba suficientemente larga."

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": fake}):
        ask("¿con qué comida me recomendás el vino tinto?")
        second = ask("¿y para un postre qué me sugerís?")

    assert len(calls) == 1
    assert "menú" in second["message_body"]


def test_very_long_messages_and_markup_are_handled():
    with patch.dict(demo_assistant._PROVIDERS, {"gemini": lambda *a: "**Hola** # título\ncon   saltos y más texto."}):
        assert ask("¿hacen regalos empresariales?")["message_body"] == "Hola título con saltos y más texto."

    def boom(*args):
        raise AssertionError("too long to send")

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": boom}):
        assert "menú" in ask("vino " * 200)["message_body"]


def test_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("DEMO_LLM_ENABLED", "false")

    def boom(*args):
        raise AssertionError("disabled")

    with patch.dict(demo_assistant._PROVIDERS, {"gemini": boom}):
        assert "menú" in ask("¿con qué comida me recomendás el vino tinto?")["message_body"]


def test_huggingface_router_tries_the_next_model_and_never_logs_the_token(monkeypatch, caplog):
    import requests

    calls = []

    class Reply:
        def __init__(self, status, body=None):
            self.status_code, self._body = status, body or {}

        def json(self):
            return self._body

    def fake_post(url, headers, json, timeout):
        calls.append((url, json["model"], headers["Authorization"]))
        if len(calls) == 1:
            return Reply(404)
        return Reply(200, {"choices": [{"message": {"content": "Te recomiendo el Malbec Reserva."}}]})

    monkeypatch.setattr("services.huggingface_inference_service._api_token", lambda: "hf_secreto")
    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.delenv("HUGGINGFACE_DEMO_CHAT_MODEL", raising=False)

    with caplog.at_level("WARNING"):
        answer = demo_assistant._ask_huggingface("sistema", "pregunta", 5.0)

    assert answer == "Te recomiendo el Malbec Reserva."
    assert len(calls) == 2 and calls[0][1] != calls[1][1]
    assert "router.huggingface.co" in calls[0][0]
    assert "hf_secreto" not in caplog.text
    assert "status=404" in caplog.text


def test_free_providers_are_tried_before_the_low_cost_one(monkeypatch):
    monkeypatch.delenv("DEMO_LLM_PROVIDERS", raising=False)
    order = []

    def none(name):
        def provider(*args):
            order.append(name)
            return None
        return provider

    def openai(*args):
        order.append("openai")
        return "Te recomiendo el Malbec Reserva para el asado."

    with patch.dict(
        demo_assistant._PROVIDERS,
        {"gemini": none("gemini"), "huggingface": none("huggingface"), "openai": openai},
    ):
        answer = ask("quiero regalarle algo a mi suegro que le gusta el asado")

    assert order == ["gemini", "huggingface", "openai"]
    assert "Malbec Reserva" in answer["message_body"]
