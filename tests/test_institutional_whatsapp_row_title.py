from services.institutional_assistant_whatsapp import row_title


def test_pictogram_stays_when_the_label_fits():
    assert row_title('1. ', '💳 Pensión RUPE') == '1. 💳 Pensión RUPE'
    assert row_title('', '📍 Ushuaia') == '📍 Ushuaia'


def test_words_win_over_the_pictogram_when_space_runs_out():
    assert row_title('0. ', '🤝 Contactos por ciudad') == '0. Contactos por ciudad'
    assert row_title('3. ', '💼 Trabajo e inclusión laboral') == '3. Trabajo e inclusión…'
    assert row_title('9. ', '← Volver a otros temas') == '9. Volver a otros temas'


def test_titles_never_exceed_the_whatsapp_limit_and_plain_labels_are_unchanged():
    for prefix, label in [('12. ', '🛒 Tarjeta Bienestar y Módulo Alimentario'), ('', 'Documentación y requisitos para el trámite'),
                          ('1. ', '¿Hay un orden de prioridad?')]:
        assert len(row_title(prefix, label)) <= 24
    assert row_title('1. ', 'Documentación y requisitos') == '1. Documentación y…'
    assert row_title('1. ', '¿Puedo inscribirme?') == '1. ¿Puedo inscribirme?'
