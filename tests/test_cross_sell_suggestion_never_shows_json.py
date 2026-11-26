"""A person must never read the raw JSON the model answers with."""

from services.pymes import _clean_cross_sell_suggestion as clean


def test_no_suggestion_is_silence():
    assert clean('{"suggestion": null}') is None
    assert clean('{"suggestion":null}') is None
    assert clean({"suggestion": None}) is None
    assert clean('```json\n{"suggestion": null}\n```') is None
    assert clean("null") is None
    assert clean("") is None
    assert clean(None) is None


def test_broken_json_is_silence_not_text():
    assert clean('{"suggestion": ') is None
    assert clean("[1, 2]") is None


def test_real_suggestion_comes_out_as_plain_text():
    assert clean('{"suggestion": "¿Coordinamos el envío?"}') == "¿Coordinamos el envío?"
    assert clean({"suggestion": "¿Coordinamos el envío?"}) == "¿Coordinamos el envío?"
    assert clean('"¿Coordinamos el envío?"') == "¿Coordinamos el envío?"
    assert clean('{"suggestion": "{\\"suggestion\\": \\"Sumá cinta\\"}"}') == "Sumá cinta"
