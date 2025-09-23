import pytest
from services.common_utils import _get_main_menu_payload
from services.constants import CONTEXTO_MUNICIPIO, ConversationState

@pytest.fixture
def base_context():
    """Provides a basic context dictionary for testing menu generation."""
    return {
        "viewer_user_obj": None,
        "profile_name": "Tester",
        "user_obj": None,
        "channel": "web",
        "chat_db_context_data": {
            CONTEXTO_MUNICIPIO: {
                "estado_conversacion": ConversationState.ESPERANDO_SELECCION_MENU_PRINCIPAL.name
            }
        }
    }

def test_main_menu_payload_structure(base_context):
    """
    Tests that the main menu payload has the correct basic structure.
    """
    payload = _get_main_menu_payload(base_context)

    assert "message_body" in payload
    assert "options_list" in payload
    assert "message_type" in payload
    assert "categorias" in payload  # New key for structured menus
    assert isinstance(payload["message_body"], str)
    assert isinstance(payload["options_list"], list)
    assert payload["message_type"] == "interactive_list"
    assert "audio_text" in payload
    assert isinstance(payload["audio_text"], str)
def test_main_menu_options_are_valid(base_context):
    """
    Tests that the options in the main menu are well-formed.
    """
    payload = _get_main_menu_payload(base_context)
    options = payload.get("options_list", [])

    assert len(options) > 0  # Ensure there are options

    for option in options:
        assert isinstance(option, dict)
        assert "texto" in option
        assert "id" in option # The new structure uses 'id' for the action
        assert isinstance(option["texto"], str)
        assert isinstance(option["id"], str)

def test_main_menu_has_help_option(base_context):
    """
    Tests that a 'Help' or 'Ayuda' option is available for accessibility.
    """
    payload = _get_main_menu_payload(base_context)
    options = payload.get("options_list", [])

    # The new menu structure has an "Ayuda" category with an action_id 'mostrar_menu_ayuda'
    help_option_found = any(opt.get("id") == "mostrar_menu_ayuda" for opt in options)

    assert help_option_found, "The main menu should contain a 'Help' (Ayuda) option."

def test_main_menu_asks_for_name_if_unknown(base_context):
    """
    Tests that the bot asks for the user's name if it's not in the context.
    """
    # Remove name from context
    base_context["profile_name"] = None

    payload = _get_main_menu_payload(base_context)

    assert payload["fuente"] == "pedir_nombre_inicial"
    assert "¿podrías decirme tu nombre?" in payload["message_body"]
    assert payload["message_type"] == "text"


def test_main_menu_reduced_does_not_repeat_intro(base_context):
    """Reduced menus should avoid repetir la introducción extensa."""
    payload = _get_main_menu_payload(
        base_context,
        welcome_message_override="¡Gracias, Test!",
        reduced=True,
    )

    assert payload["message_type"] == "interactive_list"
    assert "¡Gracias, Test!" in payload["message_body"]
    assert "Soy *JUNI*" not in payload["message_body"]
    assert "Volvimos al menú principal" in payload["audio_text"]


def test_main_menu_uses_contact_name_if_available(base_context):
    base_context["profile_name"] = None
    chat_ctx = base_context.setdefault("chat_db_context_data", {})
    municipal_ctx = chat_ctx.setdefault(CONTEXTO_MUNICIPIO, {})
    municipal_ctx["contacto_usuario"] = {"nombre": "Marcelo"}

    payload = _get_main_menu_payload(base_context)

    assert "Marcelo" in payload["message_body"]
    assert payload.get("fuente") != "pedir_nombre_inicial"
    assert "Marcelo" in payload["audio_text"]


def test_main_menu_audio_text_lists_categories(base_context):
    payload = _get_main_menu_payload(base_context)

    audio_text = payload["audio_text"].lower()
    assert "opción 1" in audio_text
    assert "reclamos y consultas" in audio_text
    assert "emojis" in audio_text
