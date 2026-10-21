"""Public confirmations need no contact name and preserve ticket information."""

import pytest

from services.ticket_utils import formatear_ticket_respuesta


@pytest.mark.parametrize("nombre", [None, "", " \t\n "])
@pytest.mark.parametrize(
    "tipo, titulo",
    [
        ("chat", "Chat en vivo recibido"),
        ("reclamo", "Reclamo recibido"),
        ("sugerencia", "Sugerencia recibida"),
        ("pedido", "Pedido recibido"),
    ],
)
def test_confirmation_without_name_keeps_ticket_and_no_placeholder(nombre, tipo, titulo):
    message, buttons = formatear_ticket_respuesta(
        tipo, nombre, "", "Atención", "M-123456"
    )

    assert message.splitlines()[0] == f"✅ *¡{titulo}!*"
    assert "None" not in message
    assert "• *Ticket:* `M-123456`" in message
    assert "• *Categoría:* Atención" in message
    assert buttons == []
    if tipo == "reclamo":
        assert "Listo ✅ Tu reclamo quedó cargado con el número `M-123456`." in message


@pytest.mark.parametrize("tipo", ["chat", "reclamo", "sugerencia", "pedido"])
def test_confirmation_preserves_valid_name(tipo):
    message, _ = formatear_ticket_respuesta(
        tipo, "  María Sol  ", "", "Atención", "M-123456"
    )

    assert message.splitlines()[0].endswith(", María Sol!*")
    assert "• *Ticket:* `M-123456`" in message
    if tipo == "reclamo":
        assert "Listo María Sol ✅" in message
