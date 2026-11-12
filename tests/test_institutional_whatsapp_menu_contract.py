"""Local formatter/inbound contract; no provider activation or delivery evidence."""
from copy import deepcopy
from unittest.mock import patch

from routes.whatsapp_webhook import _resolve_whatsapp_menu_selection
from services.response_formatter import build_interactive_response


def format_reply(reply):
    return build_interactive_response(
        options=[], body_text=reply['message_body'], channel='whatsapp',
        message_type=reply.get('message_type', 'text'),
        original_bot_response=deepcopy(reply),
    )


def test_explicit_nine_is_displayed_without_positional_menu_or_navigation():
    reply = {'message_body': 'Respuesta documentada.', 'fuente': 'institutional_knowledge',
             'message_type': 'interactive_buttons', 'botones': [
                 {'texto': 'Volver', 'action_id': 'knowledge:synthetic:start', 'reply_code': '9'}]}
    # The institutional contract also holds when interactive legacy menus are enabled.
    with patch('services.response_formatter.WHATSAPP_FORCE_TEXT', False):
        result = format_reply(reply)
    assert result['type'] == 'text'
    assert result['text']['body'] == 'Respuesta documentada.\n\n*9*. Volver'
    context = result['contexto_actualizado']
    assert context['last_options_scope'] == 'institutional_knowledge'
    assert context['last_options_sent'] == reply['botones']
    for raw in ('9', '1', 'Volver'):
        assert _resolve_whatsapp_menu_selection(raw, context) == (None, None)


def test_multinode_choices_remain_unnumbered_in_actual_whatsapp_formatter():
    result = format_reply({'message_body': 'Dos temas.', 'fuente': 'institutional_knowledge',
                          'message_type': 'interactive_buttons', 'botones': [
                              {'texto': 'Requisitos', 'action_id': 'knowledge:synthetic:requirements'},
                              {'texto': 'Volver', 'action_id': 'knowledge:synthetic:start'}]})
    assert result['text']['body'] == 'Dos temas.\n\n- Requisitos\n- Volver'
    assert _resolve_whatsapp_menu_selection('1', result['contexto_actualizado']) == (None, None)


def test_institutional_error_clears_old_options_then_legacy_scope_restores():
    legacy = {'last_options_sent': [{'texto': 'Confirmar', 'action_id': 'confirmar'}],
              'last_options_scope': 'legacy'}
    for source in ('institutional_knowledge_stale', 'institutional_knowledge_unknown_choice',
                   'institutional_knowledge_unavailable'):
        error = format_reply({'message_body': 'Elegí nuevamente un tema.', 'fuente': source})
        merged = {**legacy, **error['contexto_actualizado']}
        assert error['text']['body'] == 'Elegí nuevamente un tema.'
        assert merged['last_options_sent'] == []
        assert _resolve_whatsapp_menu_selection('1', merged) == (None, None)
    for force_text in (True, False):
        with patch('services.response_formatter.WHATSAPP_FORCE_TEXT', force_text):
            restored = format_reply({'message_body': 'Operación.', 'fuente': 'operational',
                                     'message_type': 'interactive_buttons',
                                     'botones': [{'texto': 'Continuar', 'action_id': 'continuar'}]})
        merged.update(restored['contexto_actualizado'])
        assert merged['last_options_scope'] == 'legacy'
        selected, action = _resolve_whatsapp_menu_selection('1', merged)
        assert selected['texto'] == 'Continuar'
        assert action == 'continuar'
        assert _resolve_whatsapp_menu_selection('1', merged, expecting_free_info=True) == (None, None)


def test_legacy_response_without_options_resets_scope_and_request_text_cannot_select_scope():
    reply = {'message_body': 'Recibo.', 'fuente': 'operational',
             '_suppress_whatsapp_navigation': True}
    result = format_reply(reply)
    assert result['contexto_actualizado']['last_options_scope'] == 'legacy'
    option = {'texto': 'knowledge:request-hint', 'action_id': 'confirmar'}
    assert _resolve_whatsapp_menu_selection('1', {'last_options_sent': [option]}) == (option, 'confirmar')
