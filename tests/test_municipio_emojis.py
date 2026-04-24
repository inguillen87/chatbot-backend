import pytest

from services.municipio_responder import (
    ConversationState,
    find_reclamo_category_by_input,
    _can_use_global_emoji_shortcuts,
)


@pytest.mark.parametrize(
    "emoji_input,expected_category",
    [
        ("🐶", "Otros"),
        ("🔥", "Otros"),
    ],
)
def test_reclamo_emojis_route_to_categories(emoji_input, expected_category):
    options = [{"texto": "Luminaria"}, {"texto": "Otros"}]
    assert find_reclamo_category_by_input(emoji_input, options) == expected_category


def test_emoji_shortcuts_allowed_while_waiting_initial_name():
    assert _can_use_global_emoji_shortcuts(None)
    assert _can_use_global_emoji_shortcuts(
        ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
    )
    assert _can_use_global_emoji_shortcuts(
        ConversationState.ESPERANDO_NOMBRE_INICIAL.name
    )
