"""Handles the main menu flow for web/widget channels."""

from enum import Enum, auto

from services.municipio_responder import _get_main_menu_payload


class MenuState(Enum):
    """Simplified conversation states for the menu flow."""

    ESPERANDO_SELECCION_MENU_PRINCIPAL = auto()


def handle(msg, ctx):
    """Return the structured main menu used in the chat widget."""

    context = dict(ctx or {})
    context.setdefault("channel", "web")
    return _get_main_menu_payload(context)

