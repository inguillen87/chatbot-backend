from services.municipio_responder import (
    _extract_ticket_lookup_number,
    _looks_like_ticket_status_lookup,
)


def test_phone_in_new_claim_is_not_treated_as_ticket_lookup():
    text = (
        "Quiero iniciar un reclamo por semaforo caido en Av. San Martin. "
        "Mi telefono es 2615551234."
    )

    assert not _looks_like_ticket_status_lookup(text)
    assert _extract_ticket_lookup_number(text) is None


def test_explicit_ticket_status_lookup_keeps_ticket_number():
    text = "Quiero consultar el estado del ticket M-123456"

    match = _extract_ticket_lookup_number(text)

    assert _looks_like_ticket_status_lookup(text)
    assert match is not None
    assert match.group(1) == "123456"
