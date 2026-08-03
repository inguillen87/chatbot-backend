import logging
from unittest.mock import patch

from services.municipio_responder import safe_llm_call


PRIVATE_PROMPT = "DNI 32877851, vivo en Don Bosco 56 y mi telefono es +5492613168608"
PRIVATE_PREAMBLE = "tenant-secret-preamble"
PRIVATE_RESPONSE = "respuesta privada para marcelo@example.test"


def _municipal_logs(caplog) -> str:
    return "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "services.municipio_responder"
    )


def test_safe_llm_call_logs_only_lengths_not_prompt_or_response(caplog):
    caplog.set_level(logging.DEBUG, logger="services.municipio_responder")

    with patch(
        "services.municipio_responder.get_cohere_response",
        return_value=PRIVATE_RESPONSE,
        create=True,
    ):
        assert safe_llm_call(PRIVATE_PROMPT, PRIVATE_PREAMBLE) == PRIVATE_RESPONSE

    emitted = _municipal_logs(caplog)
    assert PRIVATE_PROMPT not in emitted
    assert PRIVATE_PREAMBLE not in emitted
    assert PRIVATE_RESPONSE not in emitted
    assert f"prompt_length={len(PRIVATE_PROMPT)}" in emitted
    assert f"response_length={len(PRIVATE_RESPONSE)}" in emitted


def test_safe_llm_call_logs_exception_type_without_exception_message(caplog):
    caplog.set_level(logging.ERROR, logger="services.municipio_responder")
    private_error = RuntimeError(PRIVATE_PROMPT)

    with patch(
        "services.municipio_responder.get_cohere_response",
        side_effect=private_error,
        create=True,
    ):
        response = safe_llm_call(PRIVATE_PROMPT, PRIVATE_PREAMBLE, fallback="seguro")

    assert response == "seguro"
    emitted = _municipal_logs(caplog)
    assert PRIVATE_PROMPT not in emitted
    assert PRIVATE_PREAMBLE not in emitted
    assert "error_type=RuntimeError" in emitted
